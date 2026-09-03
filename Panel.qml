import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// Keepalive: the agent sessions bar widget for Omarchy.
//
// Shows every durable omarchy-agent-session record: who needs you, what got
// orphaned, who is working, what finished today. Structure follows
// 03-sessions-panel.md, collapsed to two files: the Process/Timer/FileView
// data layer lives here (as the Hermes reference plugin does), Session.qml
// is one row.
//
// Revised 2026-09-02 after the ux-review / design-qa pass on the rig
// (findings/evaluation-slice1-2026-09-02.md): every action runs through a
// Process and reports its exit code into the row; the cursor is tracked by
// session id and scrolled into view; Orphaned sits above Working and counts
// toward the hero and the glyph; a Herdr-down row comes from the
// reconciler's index.json; Done today collapses; a key legend sits under
// the list; no color the panel chooses sits under 3:1.
//
// Sources read for this file: omacom/omarchy quattro
// shell/plugins/agents/{Panel.qml,Main.qml}, shell/Ui/{CursorSurface,
// KeyboardPanel,PanelKeyCatcher,PanelHero,PanelSectionHeader}.qml on the
// rig; stevequinn/omarchy-hermes-sessions Panel.qml; spec/03, spec/02.
Panel {
  id: root
  moduleName: "io.github.mphaxise.keepalive"
  ipcTarget: "io.github.mphaxise.keepalive"
  manageIpc: false

  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color urgentColor: bar ? bar.urgent : Color.urgent
  readonly property color accentColor: Color.accent
  // Caption text: 1.55 gave #6d728a, 3.60:1 on Tokyo Night; 1.3 gives
  // #8288a5, 4.89:1 (WCAG AA for small text).
  readonly property color dim: Qt.darker(foreground, 1.3)
  // Dots and the rest-state glyph: 3.82:1 on Tokyo Night, above the 3:1
  // WCAG 1.4.11 asks of a control's boundary and a state graphic.
  // Color.muted (1.91:1 here) is no longer used for anything a person
  // must perceive.
  readonly property color restColor: Qt.darker(foreground, 1.5)
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property string logTag: "agent-sessions"

  function alpha(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  // ------------------------------------------------------------- settings

  function setting(name, fallback) {
    var value = root.settings ? root.settings[name] : undefined
    return value === undefined || value === null ? fallback : value
  }

  readonly property int refreshIntervalSec: Math.max(5, Number(setting("refreshIntervalSec", 10)))
  readonly property bool showWhenEmpty: setting("showWhenEmpty", false) === true
  readonly property int maxRows: Math.max(5, Number(setting("maxRows", 20)))
  readonly property int doneRowsCollapsed: Math.max(0, Number(setting("doneRowsCollapsed", 2)))
  readonly property bool motionReduced: String(setting("motion", "full")) === "reduced"
  readonly property string newSessionDir: String(setting("newSessionDir", "~/Work"))
  readonly property string newSessionMode: String(setting("newSessionMode", "personal"))

  // ------------------------------------------------------------ selection
  //
  // The cursor is a session id, so a list that re-sorts under an open
  // panel never moves the cursor onto a different session (design review
  // finding 10). Keyboard state lives here, not in Session.qml, because
  // Esc has to know about an armed Stop or an open Send field regardless
  // of which row it belongs to.

  property bool cursorActive: false
  property string selectedId: ""
  property string armedStopId: ""
  property string sendOpenId: ""
  property bool newOpen: false      // the New session field under the hero
  property bool openNewNext: false  // right click on the icon: open with the field ready
  property bool doneExpanded: false

  readonly property int selectedIndex: {
    for (var i = 0; i < visibleRows.length; i++) if (visibleRows[i].id === selectedId) return i
    return visibleRows.length > 0 ? 0 : -1
  }
  readonly property var selectedSession: selectedIndex >= 0 ? visibleRows[selectedIndex] : null

  function setCursor(id, fromKeyboard) {
    if (id === "" || id === undefined) return
    if (id !== root.selectedId) root.armedStopId = ""  // a move disarms (finding 10)
    root.selectedId = String(id)
    root.cursorActive = true
  }

  function moveCursor(dy) {
    if (root.visibleRows.length === 0) return
    var idx = Math.max(0, Math.min(root.visibleRows.length - 1, root.selectedIndex + dy))
    root.setCursor(root.visibleRows[idx].id, true)
  }

  // After a snapshot, keep the cursor on the same session; if it left the
  // list, land on the row now at its old position.
  function reconcileCursor(previousIndex) {
    if (root.visibleRows.length === 0) { root.selectedId = ""; return }
    for (var i = 0; i < root.visibleRows.length; i++) if (root.visibleRows[i].id === root.selectedId) return
    var idx = Math.max(0, Math.min(root.visibleRows.length - 1, previousIndex))
    root.selectedId = root.visibleRows[idx].id
  }

  // Sessions whose Stop was confirmed and whose record has not yet reported
  // a terminal state; Session.qml renders the spinner from this. Cleared by
  // the record reaching a terminal state or by the stop command failing.
  property var stoppingIds: ({})
  Timer {
    id: stopFollowUp
    interval: 1000; repeat: true; running: Object.keys(root.stoppingIds).length > 0
    onTriggered: root.refresh()
  }

  property double nowMs: Date.now()
  Timer {
    interval: 30000
    running: root.opened
    repeat: true
    onTriggered: root.nowMs = Date.now()
  }

  onOpenedChanged: if (opened) {
    // Cursor visible from the first frame, on the row that needs you most.
    cursorActive = true
    armedStopId = ""
    sendOpenId = ""
    newOpen = openNewNext
    openNewNext = false
    nowMs = Date.now()
    if (visibleRows.length > 0) selectedId = visibleRows[0].id
    refresh()
    if (panelFlick) panelFlick.contentY = 0
    Qt.callLater(function() { keyCatcher.forceActiveFocus() })
  }

  // ------------------------------------------------------------- data
  //
  // Primary source: a Timer drives a Process running scripts/snapshot.sh
  // (the Hermes pattern), with a 64 KB cap, try/catch, three strikes, and a
  // 60 s backoff per 03-sessions-panel.md "Failure isolation".

  property var snapshot: null // { sessions: [...] } once a good parse lands
  property bool loading: false
  property int consecutiveFailures: 0
  readonly property int maxSnapshotBytes: 65536
  readonly property int backoffIntervalSec: 60

  readonly property var allSessions: snapshot && Array.isArray(snapshot.sessions) ? snapshot.sessions : []
  readonly property bool hasErrorRow: !!snapshot && typeof snapshot.error === "string" && snapshot.error !== ""

  function scriptPath(name) {
    return Qt.resolvedUrl("scripts/" + name).toString().replace(/^file:\/\//, "")
  }

  function refresh() {
    if (snapshotProcess.running) return
    loading = true
    snapshotProcess.command = [scriptPath("snapshot.sh")]
    snapshotProcess.running = true
  }

  Process {
    id: snapshotProcess
    running: false

    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        root.loading = false
        var raw = String(text)
        if (raw.length > root.maxSnapshotBytes) {
          console.warn(root.logTag, "snapshot too large, discarded:", raw.length, "bytes")
          root.recordFailure()
          return
        }
        var parsed = null
        try {
          parsed = JSON.parse(raw)
        } catch (e) {
          console.warn(root.logTag, "bad snapshot", e)
          root.recordFailure()
          return
        }
        if (!parsed || typeof parsed !== "object" || typeof parsed.error === "string") {
          console.warn(root.logTag, "snapshot reported an error:", parsed && parsed.error)
          root.recordFailure()
          return
        }
        // Fresh clock with every list: a record whose `since` is newer than
        // the last nowMs read as a negative age (the hero's "0M", rig e5).
        var previousIndex = root.selectedIndex
        root.nowMs = Date.now()
        root.snapshot = { sessions: Array.isArray(parsed.sessions) ? parsed.sessions : [] }
        root.clearStoppedFromStopping(root.snapshot.sessions)
        root.reconcileCursor(previousIndex)
        root.recordSuccess()
      }
    }

    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (String(text).trim() !== "") console.warn(root.logTag, String(text).trim())
    }

    onExited: function(exitCode) { root.loading = false }
  }

  function recordFailure() {
    consecutiveFailures++
    if (consecutiveFailures >= 3) {
      snapshot = { sessions: [], error: "Session list unavailable, retrying" }
    }
  }

  function recordSuccess() {
    consecutiveFailures = 0
  }

  Timer {
    id: pollTimer
    interval: (root.consecutiveFailures >= 3 ? root.backoffIntervalSec : root.refreshIntervalSec) * 1000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Secondary source: the reconciler rewrites index.json on every run
  // (every 5 s from the watcher) with generated_at and the Herdr state, so
  // its age is a liveness signal and its `herdr` field is the one place
  // the panel learns the server is down.
  FileView {
    id: indexFile
    path: (Quickshell.env("XDG_STATE_HOME") || (Quickshell.env("HOME") + "/.local/state")) + "/omarchy/sessions/index.json"
    watchChanges: true
    printErrors: false
    onFileChanged: reload()
    onLoaded: root.parseIndex(text())
    onLoadFailed: { root.indexGeneratedAtMs = 0; root.herdrState = "unknown" }
  }

  property real indexGeneratedAtMs: 0
  property string herdrState: "unknown" // running | unreachable | unknown

  function parseIndex(content) {
    try {
      var parsed = JSON.parse(String(content || ""))
      var iso = parsed && parsed.generated_at ? String(parsed.generated_at) : ""
      var ms = iso !== "" ? new Date(iso).getTime() : NaN
      root.indexGeneratedAtMs = isFinite(ms) ? ms : 0
      root.herdrState = parsed && parsed.herdr ? String(parsed.herdr) : "unknown"
    } catch (e) {
      console.warn(root.logTag, "bad index.json", e)
      root.indexGeneratedAtMs = 0
      root.herdrState = "unknown"
    }
  }

  readonly property bool indexStale: indexGeneratedAtMs > 0
    && (nowMs - indexGeneratedAtMs) > (2 * root.refreshIntervalSec * 1000)
  // A stale index means the reconciler stopped; its last word on Herdr is
  // then a guess, so the Herdr-down row shows only on a fresh index.
  readonly property bool herdrDown: herdrState === "unreachable" && !indexStale

  // One duration vocabulary for the hero, the stale marker, and every row
  // (Session.qml calls this through `formatAge`): "just now", "3m",
  // "1h 20m", "2d 3h". Never "0m", never negative.
  function formatDuration(ms) {
    if (!(ms > 0)) return "just now"
    var minutes = Math.floor(ms / 60000)
    if (minutes < 1) return "just now"
    var hours = Math.floor(minutes / 60)
    var days = Math.floor(hours / 24)
    if (days > 0) return days + "d " + (hours % 24) + "h"
    if (hours > 0) return hours + "h " + (minutes % 60) + "m"
    return minutes + "m"
  }

  function staleAgeText() {
    return indexStale ? formatDuration(nowMs - indexGeneratedAtMs) : ""
  }

  // --------------------------------------------------------------- rows

  function sinceMs(session) {
    var iso = session && session.status ? String(session.status.since || "") : ""
    var ms = iso !== "" ? new Date(iso).getTime() : NaN
    return isFinite(ms) ? ms : 0
  }

  function attentionRank(state) {
    return state === "blocked" ? 0 : (state === "waiting" ? 1 : 2)
  }

  // 1. Needs you: blocked, waiting; the longest-neglected session leads.
  function needsYouSessions() {
    var list = root.allSessions.filter(function(s) {
      return s.status && (s.status.state === "blocked" || s.status.state === "waiting")
    })
    list.sort(function(a, b) {
      var r = root.attentionRank(a.status.state) - root.attentionRank(b.status.state)
      return r !== 0 ? r : root.sinceMs(a) - root.sinceMs(b)
    })
    return list
  }

  // 2. Orphaned: above Working, because a session that lost its pane is
  // the state this product exists to protect (finding 7).
  function orphanedSessions() {
    var list = root.allSessions.filter(function(s) { return s.status && s.status.state === "orphaned" })
    list.sort(function(a, b) { return root.sinceMs(b) - root.sinceMs(a) })
    return list
  }

  // 3. Working: starting, working, idle. Most recently changed first.
  function workingSessions() {
    var states = { starting: true, working: true, idle: true }
    var list = root.allSessions.filter(function(s) { return s.status && states[s.status.state] })
    list.sort(function(a, b) { return root.sinceMs(b) - root.sinceMs(a) })
    return list
  }

  // 4. Done today: done, failed, stopped in the last 24 h, newest first.
  function doneTodaySessions() {
    var states = { done: true, failed: true, stopped: true }
    var cutoff = root.nowMs - 24 * 3600 * 1000
    var list = root.allSessions.filter(function(s) {
      return s.status && states[s.status.state] && root.sinceMs(s) >= cutoff
    })
    list.sort(function(a, b) { return root.sinceMs(b) - root.sinceMs(a) })
    return list
  }

  readonly property var needsYouRows: needsYouSessions()
  readonly property var orphanedRows: orphanedSessions()
  readonly property var workingRows: workingSessions()
  readonly property var doneRows: doneTodaySessions()
  readonly property var doneShownRows: doneExpanded ? doneRows : doneRows.slice(0, doneRowsCollapsed)
  readonly property int doneHiddenCount: doneRows.length - doneShownRows.length

  // maxRows caps the flattened total; Done today is concatenated last, so
  // it is what gets trimmed.
  readonly property var visibleRows: needsYouRows.concat(orphanedRows).concat(workingRows).concat(doneShownRows).slice(0, root.maxRows)

  readonly property int needsAttentionCount: allSessions.filter(function(s) { return s.needs_attention === true }).length
  readonly property int workingCount: allSessions.filter(function(s) { return s.status && (s.status.state === "working" || s.status.state === "starting") }).length
  readonly property int idleCount: allSessions.filter(function(s) { return s.status && s.status.state === "idle" }).length

  // Bar glyph: urgent when someone needs you; foreground when a session
  // is orphaned (that needs a person too, without the badge); the rest
  // color otherwise. Three honest looks, all at 3:1 or better.
  readonly property string barState: {
    if (needsYouRows.length > 0) return "needs-you"
    if (orphanedRows.length > 0) return "orphaned"
    return "quiet"
  }
  readonly property color barGlyphColor: barState === "needs-you" ? urgentColor
    : (barState === "orphaned" ? foreground : restColor)

  visible: allSessions.length > 0 || showWhenEmpty || hasErrorRow
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  // --------------------------------------------------------------- hero

  function heroTitle() {
    if (root.hasErrorRow) return "Sessions"
    if (root.needsYouRows.length > 0) return root.needsYouRows[0].name || root.needsYouRows[0].id
    if (root.orphanedRows.length > 0) return root.orphanedRows.length + " orphaned"
    if (root.allSessions.length === 0) return "No sessions"
    return "Nothing needs you"
  }

  function heroMeta() {
    var base
    if (root.hasErrorRow) {
      base = "list unavailable"
    } else if (root.needsYouRows.length > 0) {
      var first = root.needsYouRows[0]
      base = "needs you · " + root.formatDuration(root.nowMs - root.sinceMs(first)) + " · Enter opens"
      if (root.needsYouRows.length > 1) base += " · " + (root.needsYouRows.length - 1) + " more need you"
    } else if (root.orphanedRows.length > 0) {
      base = root.herdrDown ? "Herdr is not running · Enter revives" : "Enter revives"
    } else if (root.allSessions.length === 0) {
      base = "n starts one"
    } else {
      var parts = []
      if (root.workingCount > 0) parts.push(root.workingCount + " working")
      if (root.idleCount > 0) parts.push(root.idleCount + " idle")
      if (root.doneRows.length > 0) parts.push(root.doneRows.length + " done today")
      base = parts.length > 0 ? parts.join(" · ") : "all quiet"
    }
    return root.indexStale ? (base + " · stale (" + root.staleAgeText() + ")") : base
  }

  // ------------------------------------------------------------ actions
  //
  // Every command runs through a Process and reports its exit code back
  // into the row it belongs to (finding 4). Exit codes are the CLI's:
  // 3 not found, 4 Herdr unreachable, 5 the session's state forbids it.

  property var busyById: ({})     // id -> "opening…" | "reviving…" | "sending…" | "stopping…"
  property var resultById: ({})   // id -> {text, ok, ts}

  function setBusy(id, text) {
    var next = Object.assign({}, root.busyById)
    if (text) next[id] = text; else delete next[id]
    root.busyById = next
  }

  function setResult(id, text, ok) {
    var next = Object.assign({}, root.resultById)
    if (text) next[id] = { text: text, ok: ok, ts: Date.now() }; else delete next[id]
    root.resultById = next
  }

  Timer {
    // Results stay on the row for five seconds, then the state returns.
    interval: 1000; repeat: true; running: Object.keys(root.resultById).length > 0
    onTriggered: {
      var now = Date.now(), next = {}, changed = false
      for (var id in root.resultById) {
        if (now - root.resultById[id].ts < 5000) next[id] = root.resultById[id]; else changed = true
      }
      if (changed) root.resultById = next
    }
  }

  function reason(code) {
    if (code === 3) return "session not found"
    if (code === 4) return "Herdr is not running"
    if (code === 5) return "the session's state forbids it"
    if (code === 6) return "no default agent · omarchy default agent <name>"
    if (code === 7) return "directory not found · " + root.newSessionDir
    return "exit " + code
  }

  Component {
    id: actionProcessComponent
    Process {
      property string sid: ""
      property string kind: ""
      running: false
      stderr: StdioCollector {
        waitForEnd: true
        onStreamFinished: if (String(text).trim() !== "") console.warn(root.logTag, kind, String(text).trim())
      }
      onExited: function(exitCode) {
        root.actionFinished(sid, kind, exitCode)
        destroy()
      }
    }
  }

  function runAction(sid, kind, argv, busyText) {
    var p = actionProcessComponent.createObject(root, { sid: String(sid), kind: kind, command: argv })
    if (!p) { console.warn(root.logTag, "could not start", kind); return }
    root.setResult(sid, "", true)
    root.setBusy(sid, busyText)
    p.running = true
  }

  function actionFinished(sid, kind, code) {
    root.setBusy(sid, "")
    if (kind === "open") {
      if (code === 0) { root.close(); return }   // the terminal is the point; get the overlay out of its way
      root.setResult(sid, "couldn't open · " + root.reason(code), false)
    } else if (kind === "send") {
      if (code === 0) root.setResult(sid, "sent", true)
      else if (code === 5) root.setResult(sid, "not delivered · open it and answer there", false)
      else root.setResult(sid, "not sent · " + root.reason(code), false)
      if (code !== 0 && !root.opened) root.notifyUnseen(sid, root.resultById[sid].text)
    } else if (kind === "preview") {
      if (code === 0) { root.close(); return }
      root.setResult(sid, "no preview to show · " + root.reason(code), false)
    } else if (kind === "new") {
      // The session's terminal is open by now (new-session.sh runs `open`);
      // the panel gets out of its way and the row appears on the next poll.
      if (code === 0) { root.newOpen = false; root.refresh(); root.close(); return }
      root.setResult("new", "couldn't start · " + root.reason(code), false)
    } else if (kind === "stop") {
      if (code === 0) return                     // the spinner runs until the record reports stopped
      var next = Object.assign({}, root.stoppingIds); delete next[sid]; root.stoppingIds = next
      root.setResult(sid, "stop failed · " + root.reason(code), false)
    }
    root.refresh()
  }

  function sessionEnded(s) {
    return !!(s && s.status && (s.status.state === "done" || s.status.state === "failed" || s.status.state === "stopped"))
  }

  // Mirrors Session.qml's `revivable`: orphaned, or ended by the
  // reconciler's inference with a transcript to resume.
  function sessionRevivable(s) {
    if (!s || !s.status) return false
    if (s.status.state === "orphaned") return true
    return root.sessionEnded(s) && s.resumable === true && String(s.status.detail || "").indexOf("harness exited") === 0
  }

  function sessionById(id) {
    for (var i = 0; i < root.allSessions.length; i++) if (root.allSessions[i].id === id) return root.allSessions[i]
    return null
  }

  function openSession(id) {
    if (!id) return
    var s = root.sessionById(id)
    root.runAction(id, "open", ["omarchy-agent-session-open", String(id)], root.sessionRevivable(s) ? "reviving…" : "opening…")
  }

  function sendToSession(id, text) {
    if (!id || String(text || "").trim() === "") return
    var s = root.sessionById(id)
    var argv = ["omarchy-agent-session-send", String(id), String(text)]
    // 09-closed-loop-surfaces.md section 3: feedback typed while looking
    // at the preview travels with a capture of it. The capture has to show
    // the preview and not this panel over it, so the panel closes first
    // and the command starts once the surface is gone.
    if (s && s.preview && s.preview.value) {
      argv.push("--with-capture")
      deferredAction.sid = String(id)
      deferredAction.kind = "send"
      deferredAction.argv = argv
      deferredAction.busy = "capturing, sending…"
      root.close()
      deferredAction.restart()
      return
    }
    root.runAction(id, "send", argv, "sending…")
  }

  Timer {
    id: deferredAction
    property string sid: ""
    property string kind: ""
    property var argv: []
    property string busy: ""
    interval: 400; repeat: false
    onTriggered: root.runAction(sid, kind, argv, busy)
  }

  // A result nobody can see (the panel closed itself to take a capture)
  // goes out as a toast instead, failures only; a delivered instruction
  // shows on the row's loop count the next time the panel opens.
  function notifyUnseen(id, text) {
    var s = root.sessionById(id)
    Quickshell.execDetached(["omarchy-notification-send", "--urgency", "normal", "--glyph", "󰚩",
                             (s ? s.name : "agent session") + ": " + text])
  }

  function focusPreview(id) {
    if (!id) return
    root.runAction(id, "preview", ["omarchy-agent-session-preview", String(id), "--focus"], "")
  }

  function stopSession(id) {
    if (!id) return
    var next = Object.assign({}, root.stoppingIds); next[String(id)] = Date.now(); root.stoppingIds = next
    root.runAction(id, "stop", ["omarchy-agent-session-stop", String(id)], "")
  }

  function clearStoppedFromStopping(sessions) {
    var ids = Object.keys(root.stoppingIds)
    if (ids.length === 0) return
    var live = {}
    for (var i = 0; i < sessions.length; i++) live[sessions[i].id] = sessions[i].status ? sessions[i].status.state : ""
    var next = {}
    for (var j = 0; j < ids.length; j++) {
      var st = live[ids[j]]
      if (st && st !== "stopped" && st !== "done" && st !== "failed") next[ids[j]] = root.stoppingIds[ids[j]]
    }
    root.stoppingIds = next
  }

  function openReceipt(id) {
    if (!id) return
    // Detached on purpose: omarchy-launch-tui blocks until the terminal it
    // launched exits (evaluation run 1, 2026-09-02), so a Process here
    // would hold the panel open on "opening…" until the pager closed.
    Quickshell.execDetached(["omarchy-launch-tui", "--app-id=org.omarchy.session-receipt",
                             "omarchy-agent-session-receipt", "--pager", String(id)])
    root.close()
  }

  // New session from the panel: `n`, or the starter row under the hero,
  // opens a field; Enter runs scripts/new-session.sh, which picks the
  // default agent, creates the session in newSessionDir with the text as
  // the first prompt, and opens its terminal.
  function openNew() {
    root.armedStopId = ""
    root.sendOpenId = ""
    root.newOpen = true
  }

  function startSession(text) {
    var t = String(text || "").trim()
    root.runAction("new", "new", [scriptPath("new-session.sh"), t, root.newSessionDir, root.newSessionMode], "starting…")
  }

  // Middle click and the IPC function: the session that most recently
  // started needing you (the Needs-you section leads with the oldest).
  function mostRecentAttentionSession() {
    var list = root.needsYouSessions()
    list.sort(function(a, b) {
      var r = root.attentionRank(a.status.state) - root.attentionRank(b.status.state)
      return r !== 0 ? r : root.sinceMs(b) - root.sinceMs(a)
    })
    return list
  }

  function openMostUrgent() {
    var candidates = mostRecentAttentionSession()
    if (candidates.length > 0) root.openSession(candidates[0].id)
    else if (root.orphanedRows.length > 0) root.openSession(root.orphanedRows[0].id)
    else root.open()
  }

  IpcHandler {
    target: root.ipcTarget
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function openMostUrgent(): void { root.openMostUrgent() }
    function refresh(): string { root.refresh(); return "ok" }
  }

  // ------------------------------------------------------------ bar icon

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "󰚩"
    active: true
    activeColor: root.barGlyphColor
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.RightButton) {                 // right click: straight to the New field
        if (root.opened) root.openNew()
        else { root.openNewNext = true; root.open() }
      }
      else if (buttonCode === Qt.MiddleButton) root.openMostUrgent()
      else root.toggle()
    }

    // Badge: how many sessions need a person. Inside the bar's edge, a
    // digit at ten pixels or more (finding 12).
    Rectangle {
      visible: root.needsAttentionCount > 0
      width: Math.max(16, badgeText.implicitWidth + Style.space(8))
      height: 16
      radius: height / 2
      color: root.urgentColor
      anchors.right: parent.right
      anchors.top: parent.top
      anchors.rightMargin: -3
      anchors.topMargin: 0

      Text {
        id: badgeText
        anchors.centerIn: parent
        text: root.needsAttentionCount > 99 ? "99+" : String(root.needsAttentionCount)
        color: Color.background
        font.family: root.fontFamily
        font.pixelSize: Math.max(10, Style.font.caption)
        font.bold: true
      }
    }
  }

  // ------------------------------------------------------------ panel

  function legendText() {
    if (root.sendOpenId !== "") return "⏎ sends · esc cancels"
    if (root.armedStopId !== "") {
      var s = root.sessionById(root.armedStopId)
      return "x again stops " + (s ? s.name : "it") + " · esc cancels"
    }
    // Fits 400 px at the default font; `r` (receipt) works on every row
    // and the ended rows' own button says so, so it stays off the legend.
    if (root.newOpen) return "⏎ starts it in " + root.newSessionDir + " · esc cancels"
    // Priority order, six entries at most: that is what fits 400 px at the
    // default font (run 4, 2026-09-03). "→ more" also sits on the Done
    // today header, and esc closes every panel, so they give way first.
    var parts = ["↑↓ move", "⏎ open", "s send", "x stop", "n new"]
    var cur = root.selectedSession
    if (cur && cur.preview && cur.preview.value) parts.push("p preview")
    if (root.doneHiddenCount > 0) parts.push("→ more")
    else if (root.doneExpanded) parts.push("← fewer")
    parts.push("esc")
    return parts.slice(0, 6).join(" · ")
  }

  // Scroll the cursor row into view. Rows change height as the cursor
  // moves (the cursor row shows its actions), so this runs after layout.
  function ensureVisible(item) {
    if (!item || !panelFlick) return
    Qt.callLater(function() {
      if (!item || !panelFlick) return
      var top = item.mapToItem(column, 0, 0).y
      var bottom = top + item.height
      var viewTop = panelFlick.contentY
      var viewBottom = viewTop + panelFlick.height
      if (top < viewTop) panelFlick.contentY = Math.max(0, top - Style.space(8))
      else if (bottom > viewBottom) panelFlick.contentY = Math.min(Math.max(0, panelFlick.contentHeight - panelFlick.height), bottom - panelFlick.height + Style.space(8))
    })
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(400))
    contentHeight: panel.fittedContentHeight(column.implicitHeight + legend.height, Style.space(640))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent

      onMoveRequested: function(dx, dy) {
        if (root.sendOpenId !== "" || root.newOpen) return   // a field owns its keys
        if (dx !== 0) {
          if (dx > 0 && root.doneHiddenCount > 0) root.doneExpanded = true
          else if (dx < 0 && root.doneExpanded) root.doneExpanded = false
          return
        }
        if (dy !== 0) root.moveCursor(dy)
      }
      onActivateRequested: {
        if (root.sendOpenId !== "" || root.newOpen) return
        var s = root.selectedSession
        if (!s) return
        if (root.sessionEnded(s) && !root.sessionRevivable(s)) root.openReceipt(s.id); else root.openSession(s.id)
      }
      onDeleteRequested: {
        // PanelKeyCatcher routes `x` here, never to onTextKey.
        if (root.sendOpenId !== "" || root.newOpen) return
        var s = root.selectedSession
        if (!s) return
        var ended = s.status && (s.status.state === "done" || s.status.state === "failed" || s.status.state === "stopped")
        if (ended) return
        if (root.armedStopId === s.id) { root.stopSession(s.id); root.armedStopId = "" }
        else root.armedStopId = s.id
      }
      // Tab and Shift+Tab walk to the neighbouring bar panel, the shell's
      // convention (plugins.omarchy.org/develop.html, the built-in agents
      // plugin); a field that is open keeps the key.
      onTabRequested: function(direction) {
        if (root.sendOpenId !== "" || root.newOpen) return
        root.switchPanel(direction)
      }
      onCloseRequested: {
        // Esc clears an open Send field or an armed Stop first, and closes
        // the panel on the press after that.
        if (root.newOpen) { root.newOpen = false; return }
        if (root.sendOpenId !== "") { root.sendOpenId = ""; return }
        if (root.armedStopId !== "") { root.armedStopId = ""; return }
        root.close()
      }
      onTextKey: function(t) {
        if (root.sendOpenId !== "" || root.newOpen) return
        if (t === "n" || t === "N") { root.openNew(); return }
        var s = root.selectedSession
        if (!s) return
        var ended = s.status && (s.status.state === "done" || s.status.state === "failed" || s.status.state === "stopped")
        if (t === "s" || t === "S") {
          if (ended) return
          root.armedStopId = ""
          root.sendOpenId = s.id
        } else if (t === "r" || t === "R") {
          root.openReceipt(s.id)
        } else if (t === "p" || t === "P") {
          if (s.preview && s.preview.value) root.focusPreview(s.id)
        }
      }

      Column {
        id: outer
        anchors.fill: parent
        spacing: 0

        Flickable {
          id: panelFlick
          width: parent.width
          height: Math.max(0, outer.height - legend.height)
          contentWidth: width
          contentHeight: column.implicitHeight
          clip: true
          boundsBehavior: Flickable.StopAtBounds
          flickableDirection: Flickable.VerticalFlick
          interactive: contentHeight > height

          // Own scroll indicator: two pixels, always visible when the list
          // is longer than the panel (finding 3). The transient QQC2 bar was
          // invisible at rest in every capture.
          Rectangle {
            visible: panelFlick.contentHeight > panelFlick.height
            width: 2
            radius: 1
            color: root.dim
            x: panelFlick.width - width
            y: panelFlick.contentY + (panelFlick.height * (panelFlick.contentY / Math.max(1, panelFlick.contentHeight)))
            height: Math.max(16, panelFlick.height * (panelFlick.height / Math.max(1, panelFlick.contentHeight)))
            z: 2
          }

          Column {
            id: column
            width: panelFlick.width
            spacing: Style.space(10)

            // ---------- Hero ----------
            PanelHero {
              width: parent.width
              title: root.heroTitle()
              meta: root.heroMeta()
              foreground: root.foreground
              fontFamily: root.fontFamily

              iconComponent: Component {
                Rectangle {
                  width: 12; height: 12; radius: 6
                  color: root.barState === "orphaned" ? "transparent" : root.barGlyphColor
                  border.color: root.barGlyphColor
                  border.width: root.barState === "orphaned" ? 2 : 0
                  anchors.centerIn: parent
                }
              }
            }

            // ---------- New session: the starter row ----------
            //
            // One row under the hero. At rest it names the key and the
            // directory; `n`, a click, or the empty state's button turn it
            // into a field. Enter starts the session and opens its
            // terminal; the result of a failed start shows beneath.
            BorderSurface {
              id: starter
              visible: !root.hasErrorRow
              width: parent.width
              implicitHeight: starterColumn.implicitHeight + Style.space(16)
              color: root.newOpen ? root.alpha(root.accentColor, 0.06)
                : (starterHover.containsMouse ? root.alpha(root.foreground, 0.08) : "transparent")
              borderSpec: Border.flat(root.alpha(root.newOpen ? root.accentColor : root.foreground, root.newOpen ? 0.45 : 0.18), 1)
              radius: Style.cornerRadius

              MouseArea {
                id: starterHover
                anchors.fill: parent
                hoverEnabled: true
                enabled: !root.newOpen
                cursorShape: Qt.PointingHandCursor
                onClicked: root.openNew()
              }

              Column {
                id: starterColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                spacing: Style.space(6)

                Text {
                  visible: !root.newOpen
                  width: parent.width
                  textFormat: Text.PlainText
                  text: "n  New session in " + root.newSessionDir + "…"
                  color: root.dim
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  elide: Text.ElideRight
                }

                Row {
                  visible: root.newOpen
                  width: parent.width
                  spacing: Style.space(6)

                  TextField {
                    id: newField
                    width: parent.width - startButton.width - Style.space(6)
                    placeholderText: "What should the agent do? Enter starts it…"
                    focus: root.newOpen
                    foreground: root.foreground
                    accent: root.accentColor
                    onAccepted: { root.startSession(text); text = "" }
                    Keys.onEscapePressed: root.newOpen = false
                  }
                  Button {
                    id: startButton
                    text: "Start"
                    bordered: true
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                    onClicked: { root.startSession(newField.text); newField.text = "" }
                  }
                }

                Text {
                  visible: text !== ""
                  width: parent.width
                  textFormat: Text.PlainText
                  text: root.busyById["new"] ? root.busyById["new"]
                    : (root.resultById["new"] ? root.resultById["new"].text : "")
                  color: root.busyById["new"] ? root.dim
                    : (root.resultById["new"] && !root.resultById["new"].ok ? root.urgentColor : root.dim)
                  font.family: root.fontFamily
                  font.pixelSize: Style.font.caption
                  wrapMode: Text.WordWrap
                }
              }
            }

            // ---------- Herdr is not running ----------
            BorderSurface {
              visible: root.herdrDown && !root.hasErrorRow
              width: parent.width
              implicitHeight: herdrText.implicitHeight + Style.space(20)
              color: root.alpha(root.urgentColor, 0.10)
              borderSpec: Border.flat(root.alpha(root.urgentColor, 0.35), 1)
              radius: Style.cornerRadius

              Text {
                id: herdrText
                textFormat: Text.PlainText
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                text: "Herdr is not running. Super+Ctrl+Return starts it; a session revives with Enter once it is up."
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            // ---------- Error row (three-strikes backoff) ----------
            BorderSurface {
              visible: root.hasErrorRow
              width: parent.width
              implicitHeight: errorText.implicitHeight + Style.space(20)
              color: root.alpha(root.urgentColor, 0.10)
              borderSpec: Border.flat(root.alpha(root.urgentColor, 0.35), 1)
              radius: Style.cornerRadius

              Text {
                id: errorText
                textFormat: Text.PlainText
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: Style.space(12)
                anchors.rightMargin: Style.space(12)
                text: root.snapshot ? String(root.snapshot.error || "") : ""
                color: root.foreground
                font.family: root.fontFamily
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
              }
            }

            // ---------- No sessions ----------
            Column {
              visible: !root.hasErrorRow && root.allSessions.length === 0
              width: parent.width
              spacing: Style.space(10)
              topPadding: Style.space(16)

              Text {
                width: parent.width
                text: root.snapshot ? "No sessions yet. Press n, or click above, to start one." : "Checking for sessions…"
                color: root.dim
                font.family: root.fontFamily
                font.pixelSize: Style.font.body
                horizontalAlignment: Text.AlignHCenter
                wrapMode: Text.WordWrap
              }
            }

            // ---------- Sections ----------
            Repeater {
              model: [
                { title: "NEEDS YOU", rows: root.needsYouRows, offset: 0, hidden: 0 },
                { title: "ORPHANED", rows: root.orphanedRows, offset: root.needsYouRows.length, hidden: 0 },
                { title: "WORKING", rows: root.workingRows, offset: root.needsYouRows.length + root.orphanedRows.length, hidden: 0 },
                { title: "DONE TODAY", rows: root.doneShownRows, offset: root.needsYouRows.length + root.orphanedRows.length + root.workingRows.length, hidden: root.doneHiddenCount }
              ]

              delegate: Column {
                required property var modelData
                width: column.width
                spacing: Style.space(6)
                visible: !root.hasErrorRow && (modelData.rows.length > 0 || modelData.hidden > 0)

                PanelSeparator {
                  foreground: root.foreground
                }

                Item {
                  width: parent.width
                  height: sectionHeader.implicitHeight

                  PanelSectionHeader {
                    id: sectionHeader
                    width: parent.width
                    text: modelData.title + (modelData.hidden > 0 ? " · " + (modelData.rows.length + modelData.hidden) : "")
                    foreground: root.foreground
                    fontFamily: root.fontFamily
                  }

                  Text {
                    visible: modelData.hidden > 0
                    textFormat: Text.PlainText
                    text: modelData.hidden + " more · →"
                    color: root.dim
                    font.family: root.fontFamily
                    font.pixelSize: Style.font.caption
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: root.doneExpanded = true
                    }
                  }
                }

                Repeater {
                  model: modelData.rows

                  delegate: Session {
                    id: sessionRow
                    required property var modelData
                    required property int index

                    session: modelData
                    hasCursor: root.cursorActive && modelData.id === root.selectedId
                    emphasized: index === 0 && parent.modelData.title === "NEEDS YOU"
                    stopArmed: root.armedStopId === modelData.id
                    stopping: root.stoppingIds[modelData.id] !== undefined
                    sendOpen: root.sendOpenId === modelData.id
                    busyText: root.busyById[modelData.id] || ""
                    actionResult: root.resultById[modelData.id] || null
                    motionReduced: root.motionReduced
                    foreground: root.foreground
                    accent: root.accentColor
                    urgentColor: root.urgentColor
                    restColor: root.restColor
                    fontFamily: root.fontFamily
                    nowMs: root.nowMs
                    formatAge: root.formatDuration

                    onHasCursorChanged: if (hasCursor) root.ensureVisible(sessionRow)
                    onHoverRequested: function(id) { root.setCursor(id, false) }
                    onOpenRequested: function(id) { root.setCursor(id, false); root.openSession(id) }
                    onReceiptRequested: function(id) { root.setCursor(id, false); root.openReceipt(id) }
                    onPreviewRequested: function(id) { root.setCursor(id, false); root.focusPreview(id) }
                    onSendOpenRequested: function(id) { root.setCursor(id, false); root.armedStopId = ""; root.sendOpenId = id }
                    onSendSubmitRequested: function(id, text) { root.sendToSession(id, text); root.sendOpenId = "" }
                    onSendCancelRequested: function(id) { root.sendOpenId = "" }
                    onStopArmRequested: function(id) { root.setCursor(id, false); root.sendOpenId = ""; root.armedStopId = id }
                    onStopConfirmRequested: function(id) { root.stopSession(id); root.armedStopId = "" }
                  }
                }
              }
            }

            Item {
              width: parent.width
              height: Style.space(2)
            }
          }
        }

        // ---------- Key legend ----------
        Item {
          id: legend
          width: parent.width
          height: legendText.implicitHeight + Style.space(12)

          PanelSeparator {
            anchors.top: parent.top
            width: parent.width
            foreground: root.foreground
          }

          Text {
            id: legendText
            textFormat: Text.PlainText
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.bottom: parent.bottom
            anchors.leftMargin: Style.space(12)
            anchors.rightMargin: Style.space(12)
            anchors.bottomMargin: Style.space(2)
            text: root.legendText()
            color: root.dim
            font.family: root.fontFamily
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }
        }
      }
    }
  }
}
