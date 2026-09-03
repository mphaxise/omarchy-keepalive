import QtQuick
import qs.Commons
import qs.Ui

// One session row for the Agent Sessions panel.
//
// Two lines at rest: the name with its state and age on the right, then
// the goal (or project · branch) with the permission mode. The cursor row
// grows a third line of actions, so the list stays short and the keys are
// visible where they act (design review, 2026-09-02, findings 3 and 7).
//
// Rendering and local UI affordances only: every action a person can take
// is a signal. Panel.qml owns the omarchy-agent-session-* calls, their
// results, and the cross-row state (which row has the cursor, an armed
// Stop, an open Send field, a running action), because Esc has to clear
// any of those regardless of which row it belongs to.
//
// `hasCursor`, `foreground`, and `accent` are CursorSurface's own
// properties and are set from Panel.qml. They must not be redeclared here:
// a redeclaration shadows the base property, so CursorSurface's
// `color: hasCursor ? fill : ...` kept reading its own never-set copy and
// the keyboard cursor was never painted (rig capture, 2026-09-02 14:08).
CursorSurface {
  id: row

  // ---- inputs ----
  property var session: null          // one row object from list --json
  property bool stopArmed: false      // second x / click within this arm executes
  property bool sendOpen: false       // inline send field visible
  property bool addOpen: false        // inline add-an-agent field visible (11-agent-lanes.md)
  property string selectedLane: ""    // the lane the cursor is on inside this row; "" is the session's own agent
  property bool stopping: false       // Stop confirmed, record not yet stopped
  property bool emphasized: false     // the first Needs-you row: the eye must land here
  property string busyText: ""        // an action is running for this row ("opening…")
  property var actionResult: null     // {text, ok} from the last action, cleared by Panel.qml
  property bool motionReduced: false  // no looping spinner
  property color urgentColor: Color.urgent
  property color restColor: Qt.darker(foreground, 1.5)
  property string fontFamily: Style.font.family
  property double nowMs: Date.now()
  property var formatAge: null        // Panel.formatDuration, one vocabulary for hero and rows

  readonly property color dim: Qt.darker(foreground, 1.3) // 4.89:1 on Tokyo Night; 1.55 failed WCAG AA

  // ---- outputs ----
  signal openRequested(string id)
  signal receiptRequested(string id)
  signal sendOpenRequested(string id)
  signal sendSubmitRequested(string id, string text)
  signal sendCancelRequested(string id)
  signal stopArmRequested(string id)
  signal stopConfirmRequested(string id)
  signal pauseRequested(string id)
  signal hoverRequested(string id)
  signal previewRequested(string id)
  signal addOpenRequested(string id)
  signal addSubmitRequested(string id, string text)
  signal addCancelRequested(string id)
  signal laneSelectRequested(string id, string lane)
  signal suggestionDecided(string id, bool dismiss)

  readonly property string sid: session ? String(session.id || "") : ""
  readonly property string sname: session && session.name ? String(session.name) : sid
  readonly property string agentKind: session && session.agent ? String(session.agent.kind || "") : ""
  readonly property string state: session && session.status ? String(session.status.state || "") : ""
  readonly property string sinceIso: session && session.status ? String(session.status.since || "") : ""
  readonly property bool lowConfidence: session && session.status
    ? String(session.status.source || "") === "herdr-manifest" : false
  readonly property string branch: session && session.workspace ? String(session.workspace.branch || "") : ""
  readonly property string project: session && session.project ? String(session.project) : ""
  readonly property string goal: session && session.goal ? String(session.goal) : ""
  readonly property string mode: session && session.mode ? String(session.mode) : ""
  readonly property bool resumable: session ? session.resumable === true : false
  readonly property int childCount: session ? Number(session.children || 0) : 0
  readonly property bool hasPreview: session && session.preview && session.preview.value ? true : false
  readonly property int loopInstructions: session && session.loop ? Number(session.loop.instructions || 0) : 0
  readonly property int loopCaptures: session && session.loop ? Number(session.loop.captures || 0) : 0
  readonly property bool needsAttention: session ? session.needs_attention === true : false
  // 11-agent-lanes.md: the session's lanes, and the lane that needs you
  // when the session's own agent does not (folded in by Panel.foldLanes).
  // A delegate's `session` comes through the model as a QVariantMap, so a
  // nested array arrives as a QVariantList: it has a length and iterates,
  // and Array.isArray says false (rig, run 9, 2026-09-03: lane lines never
  // showed). Test length, never isArray, on anything read from `session`.
  readonly property var lanes: session && session.lanes && session.lanes.length !== undefined ? session.lanes : []
  // 12-two-people.md: suggestions waiting on the owner, and who has the
  // session open right now (presence, from the runtime dir, never the record).
  readonly property var suggestions: session && session.suggestions && session.suggestions.length !== undefined ? session.suggestions : []
  readonly property bool hasSuggestion: suggestions.length > 0
  // author_display is the core's name for the suggester as this person
  // should read it (user@host when the label would read as their own).
  readonly property string suggester: hasSuggestion ? String(suggestions[0].author_display || (suggestions[0].author && suggestions[0].author.label) || "someone") : "someone"
  readonly property var presence: session && session.presence && session.presence.length !== undefined ? session.presence : []
  // The owner when it is someone else; the core sends null when it is you.
  readonly property string ownedByOther: session && session.owned_by_other ? String(session.owned_by_other) : ""
  readonly property bool hasLanes: lanes.length > 0
  readonly property var attentionLane: session && session.attention_lane ? session.attention_lane : null
  readonly property bool laneNeedsYou: !needsAttention && attentionLane !== null

  readonly property bool isLive: state === "starting" || state === "working" || state === "idle"
    || state === "waiting" || state === "blocked"
  readonly property bool isOrphaned: state === "orphaned"
  // A live session with no process, parked by a person (01-session-model.md,
  // 2026-09-03): Resume, Send (queued), Stop; never Pause again.
  readonly property bool isPaused: state === "paused"
  readonly property bool isEnded: state === "done" || state === "failed" || state === "stopped"
  readonly property string statusDetail: session && session.status ? String(session.status.detail || "") : ""
  // Whether Enter brings the session back: the core decides and says so
  // as `revivable` (02-command-surface.md, 2026-09-03): orphaned; an end
  // the reconciler inferred from a vanished harness, with a transcript
  // (a reboot before the restart rule ended two sessions this way, rig
  // 2026-09-02 22:30); a stop with a transcript, since a stop is a pause.
  // The older rule stays as the fallback for a core that predates the
  // field. The receipt stays on `r` either way.
  readonly property bool revivable: (session && typeof session.revivable === "boolean")
    ? session.revivable
    : (isOrphaned || isPaused || (isEnded && resumable && statusDetail.indexOf("harness exited") === 0))
  readonly property bool isStopped: state === "stopped"

  readonly property var spinnerFrames: ["◐", "◓", "◑", "◒"]
  property int spinnerIndex: 0
  Timer {
    // 250 ms per frame, 4 fps: visible progress without a flicker. Off
    // entirely when the plugin's `motion` setting is "reduced" (the shell
    // exposes no system preference to read; 03-sessions-panel.md).
    running: row.stopping && !row.motionReduced
    interval: 250; repeat: true
    onTriggered: row.spinnerIndex = (row.spinnerIndex + 1) % row.spinnerFrames.length
  }

  function ageText() {
    var ms = sinceIso !== "" ? new Date(sinceIso).getTime() : NaN
    if (!isFinite(ms)) return ""
    var d = Math.max(0, nowMs - ms)
    if (typeof formatAge === "function") return formatAge(d)
    var mins = Math.floor(d / 60000)
    return mins < 1 ? "just now" : mins + "m"
  }

  // The state slot says what is true right now, in this order: an action
  // in flight, its result, a stop in progress, then the record's state.
  function stateText() {
    if (busyText !== "") return busyText
    if (actionResult && actionResult.text) return String(actionResult.text)
    if (stopping) return (motionReduced ? "◌" : spinnerFrames[spinnerIndex]) + " stopping…"
    var age = ageText()
    if (state === "blocked" || state === "waiting") return "needs you" + (age !== "" ? " · " + age : "")
    if (laneNeedsYou) return attentionLane.lane + " needs you"
    if (hasSuggestion && !needsAttention) return suggester + " suggests"
    if (state === "orphaned") return "orphaned · " + (resumable ? "resumes conversation" : "fresh start")
    if (state === "paused") return "paused" + (age !== "" ? " · " + age : "")
    if (isEnded && revivable) return state + " · resumes conversation"
    if (state === "starting") return "starting" + (age !== "" ? " · " + age : "")
    return state + (age !== "" ? " · " + age : "")
  }

  readonly property color stateTextColor: {
    if (busyText !== "") return foreground
    if (actionResult) return actionResult.ok ? foreground : urgentColor
    if (stopping) return urgentColor
    if (state === "blocked" || state === "waiting" || laneNeedsYou) return urgentColor
    if (hasSuggestion && !needsAttention) return accent
    if (state === "orphaned" || state === "failed") return foreground
    return dim
  }

  // Dot: color and shape together, so the state holds without color.
  // Filled urgent = needs you; ring in foreground = orphaned; ring in
  // urgent = failed; filled rest color = alive or ended quietly.
  readonly property color dotColor: (state === "blocked" || state === "waiting" || state === "failed" || laneNeedsYou)
    ? urgentColor : (state === "orphaned" ? foreground : restColor)
  readonly property bool dotHollow: state === "orphaned" || state === "failed" || state === "paused"

  readonly property string loopText: (loopInstructions > 0 || loopCaptures > 0)
    ? (loopInstructions + " instruction" + (loopInstructions === 1 ? "" : "s") + ", " + loopCaptures + " capture" + (loopCaptures === 1 ? "" : "s"))
    : ""
  // The loop count leads so a long goal cannot elide it away
  // (09-closed-loop-surfaces.md section 7).
  readonly property string suggestionText: hasSuggestion ? ("\u201c" + String(suggestions[0].text || "").split("\n")[0] + "\u201d") : ""
  readonly property string presenceText: presence.length > 1 ? (presence.length + " here") : ""
  readonly property string ownerText: ownedByOther !== "" ? ("owned by " + ownedByOther) : ""
  readonly property string detailText: [suggestionText, ownerText, loopText, goal !== "" ? goal
    : [project, branch].filter(function(t) { return t !== "" }).join(" · "), presenceText]
    .filter(function(t) { return t !== "" }).join(" · ")

  // Revive is for a session that lost its pane without anyone deciding;
  // Resume is for one a person stopped (03-sessions-panel.md, row actions).
  readonly property string openLabel: revivable ? ((isStopped || isPaused) ? "⏎ Resume" : "⏎ Revive")
    : (isEnded ? "⏎ Receipt" : (needsAttention ? "⏎ Answer" : "⏎ Open"))

  function stopLabel() {
    if (stopping) return "stopping…"
    if (!stopArmed) return "x Stop"
    return "x Confirm stop" + (childCount > 0 ? " (+" + childCount + " child" + (childCount === 1 ? "" : "ren") + ")" : "")
  }

  current: false
  bordered: false

  width: parent ? parent.width : 0
  implicitHeight: mainColumn.implicitHeight + Style.space(14)

  MouseArea {
    id: rowMouse
    anchors.fill: parent
    hoverEnabled: true
    acceptedButtons: Qt.LeftButton
    cursorShape: Qt.PointingHandCursor
    // Hover moves the panel's single cursor here (CursorSurface contract:
    // visuals derive from hasCursor, never from containsMouse).
    onContainsMouseChanged: if (containsMouse) row.hoverRequested(row.sid)
    onClicked: row.isEnded ? row.receiptRequested(row.sid) : row.openRequested(row.sid)
    z: -1 // behind the action buttons so their own clicks win
  }

  Column {
    id: mainColumn
    anchors.left: parent.left
    anchors.right: parent.right
    anchors.verticalCenter: parent.verticalCenter
    anchors.leftMargin: Style.space(12)
    anchors.rightMargin: Style.space(12)
    spacing: Style.space(3)

    // ---------- line 1: dot, name, state · age ----------
    Item {
      width: parent.width
      height: Math.max(nameText.implicitHeight, stateLabel.implicitHeight)

      Rectangle {
        id: stateDot
        width: 8; height: 8; radius: 4
        color: row.dotHollow ? "transparent" : row.dotColor
        border.color: row.dotColor
        border.width: row.dotHollow ? 2 : 0
        anchors.left: parent.left
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: nameText
        textFormat: Text.PlainText
        text: row.sname
        color: row.foreground
        font.family: row.fontFamily
        font.pixelSize: row.emphasized ? Style.font.subtitle : Style.font.body
        font.bold: row.needsAttention || row.emphasized
        elide: Text.ElideRight
        anchors.left: stateDot.right
        anchors.leftMargin: Style.space(8)
        anchors.right: lowDot.visible ? lowDot.left : stateLabel.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
      }

      // Low-confidence marker: shape plus a tooltip, never color alone.
      Rectangle {
        id: lowDot
        visible: row.lowConfidence
        width: 6; height: 6; radius: 3
        color: "transparent"
        border.color: row.dim
        border.width: 1
        anchors.right: stateLabel.left
        anchors.rightMargin: Style.space(6)
        anchors.verticalCenter: parent.verticalCenter

        MouseArea { id: dotHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton; z: 1 }
        PanelToolTip {
          visible: dotHover.containsMouse
          text: "state inferred from on-screen text; no lifecycle hook for this agent"
          fontFamily: row.fontFamily
        }
      }

      Text {
        id: stateLabel
        textFormat: Text.PlainText
        text: row.stateText()
        color: row.stateTextColor
        font.family: row.fontFamily
        font.pixelSize: row.emphasized ? Style.font.bodySmall : Style.font.caption
        font.bold: row.emphasized
        elide: Text.ElideLeft
        // Never wider than about half the row, so a long result message
        // leaves the name readable; the message elides on the left where
        // the age sits, and the whole text is in the tooltip below.
        width: Math.min(implicitWidth, Math.floor(parent.width * 0.55))
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter

        MouseArea { id: stateHover; anchors.fill: parent; hoverEnabled: true; acceptedButtons: Qt.NoButton; z: 1 }
        PanelToolTip {
          visible: stateHover.containsMouse && stateLabel.truncated
          text: row.stateText()
          fontFamily: row.fontFamily
        }
      }
    }

    // ---------- line 2: goal or project · branch, and the mode ----------
    Item {
      width: parent.width
      height: Math.max(detailLabel.implicitHeight, modeLabel.implicitHeight)
      visible: !row.sendOpen && !row.addOpen

      Text {
        id: detailLabel
        textFormat: Text.PlainText
        text: row.detailText !== "" ? row.detailText : row.agentKind
        color: row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        elide: Text.ElideRight
        anchors.left: parent.left
        anchors.leftMargin: Style.space(16)
        anchors.right: modeLabel.left
        anchors.rightMargin: Style.space(8)
        anchors.verticalCenter: parent.verticalCenter
      }

      Text {
        id: modeLabel
        textFormat: Text.PlainText
        // Personal runs with the harness's no-prompt flags; that is the
        // fact a person most needs to see at a glance, so it never hides.
        text: row.mode !== "" ? row.mode : row.agentKind
        color: row.mode === "shared" || row.mode === "restricted" ? row.accent : row.dim
        font.family: row.fontFamily
        font.pixelSize: Style.font.caption
        anchors.right: parent.right
        anchors.verticalCenter: parent.verticalCenter
      }
    }

    // ---------- lanes, cursor row only (11-agent-lanes.md) ----------
    //
    // One line per added lane: dot, name, kind, state, the task elided.
    // The lane the cursor is on carries the cursor fill; a click on a lane
    // line moves the cursor there, so Enter, s, x, and r act on the lane.
    Column {
      width: parent.width
      visible: row.hasCursor && row.hasLanes && !row.sendOpen && !row.addOpen
      spacing: Style.space(2)
      topPadding: Style.space(2)

      Repeater {
        model: row.lanes
        delegate: Rectangle {
          required property var modelData
          readonly property bool isSelected: row.selectedLane === modelData.lane
          readonly property bool laneUrgent: modelData.needs_attention === true
          width: parent.width
          height: laneText.implicitHeight + Style.space(6)
          radius: Style.cornerRadius / 2
          color: isSelected ? Qt.rgba(row.accent.r, row.accent.g, row.accent.b, 0.18) : "transparent"
          border.width: isSelected ? 1 : 0
          border.color: row.accent

          MouseArea { anchors.fill: parent; cursorShape: Qt.PointingHandCursor; onClicked: row.laneSelectRequested(row.sid, modelData.lane) }

          Rectangle {
            width: 6; height: 6; radius: 3
            anchors.left: parent.left; anchors.leftMargin: Style.space(20)
            anchors.verticalCenter: parent.verticalCenter
            color: laneUrgent ? row.urgentColor : (modelData.state === "failed" ? "transparent" : row.restColor)
            border.width: modelData.state === "failed" ? 1 : 0
            border.color: row.urgentColor
          }
          Text {
            id: laneText
            textFormat: Text.PlainText
            anchors.left: parent.left; anchors.leftMargin: Style.space(32)
            anchors.right: parent.right; anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            elide: Text.ElideRight
            text: modelData.lane + " · " + (modelData.kind || "") + " · "
              + (laneUrgent ? "needs you" : String(modelData.state || ""))
              + (modelData.task ? " · " + modelData.task : "")
            color: laneUrgent ? row.urgentColor : (isSelected ? row.foreground : row.dim)
            font.family: row.fontFamily
            font.pixelSize: Style.font.caption
          }
        }
      }
    }

    // ---------- inline Add-an-agent field ----------
    Row {
      width: parent.width
      visible: row.addOpen
      spacing: Style.space(6)

      TextField {
        id: addField
        width: parent.width - addButton.width - Style.space(6)
        placeholderText: "Add an agent: claude <its task>…"
        focus: row.addOpen
        foreground: row.foreground
        accent: row.accent
        onAccepted: { row.addSubmitRequested(row.sid, text); text = "" }
        Keys.onEscapePressed: row.addCancelRequested(row.sid)
      }
      Button {
        id: addButton
        text: "Add"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        onClicked: { row.addSubmitRequested(row.sid, addField.text); addField.text = "" }
      }
    }

    // ---------- inline Send field ----------
    Row {
      width: parent.width
      visible: row.sendOpen
      spacing: Style.space(6)

      TextField {
        id: sendField
        width: parent.width - sendButton.width - Style.space(6)
        placeholderText: row.selectedLane !== "" ? ("Send to lane " + row.selectedLane + "…")
          : (row.isOrphaned ? "Queue an instruction, delivered on revive…"
          : (row.isPaused ? "Queue an instruction, delivered on resume…"
          : (row.hasPreview ? "Feedback on the preview (a capture goes with it)…"
          : (row.hasLanes ? "Send to every lane…" : "Send an instruction…"))))
        focus: row.sendOpen
        foreground: row.foreground
        accent: row.accent
        onAccepted: { row.sendSubmitRequested(row.sid, text); text = "" }
        Keys.onEscapePressed: row.sendCancelRequested(row.sid)
      }
      Button {
        id: sendButton
        text: "Send"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        onClicked: { row.sendSubmitRequested(row.sid, sendField.text); sendField.text = "" }
      }
    }

    // ---------- line 3, cursor row only: the actions ----------
    // A Flow, since 2026-09-03: five buttons on a live row (Open, Send,
    // Pause, Stop, Add) wrap to a second line at 400 px instead of running
    // past the panel's edge.
    Flow {
      width: parent.width
      spacing: Style.space(8)
      visible: row.hasCursor && !row.sendOpen && !row.addOpen
      topPadding: Style.space(4)

      Button {
        text: row.openLabel
        bordered: true
        enabled: !row.stopping && row.busyText === ""
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: (row.isEnded && !row.revivable) ? row.receiptRequested(row.sid) : row.openRequested(row.sid)
      }
      Button {
        visible: row.isEnded && row.revivable
        text: "r Receipt"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.receiptRequested(row.sid)
      }
      Button {
        visible: row.isLive || row.isOrphaned || row.isPaused
        text: "s Send"
        bordered: true
        enabled: !row.stopping && row.busyText === ""
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.sendOpenRequested(row.sid)
      }
      Button {
        visible: row.hasSuggestion
        text: "y Accept"
        bordered: true
        foreground: row.accent
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.suggestionDecided(row.sid, false)
      }
      Button {
        visible: row.hasSuggestion
        text: "d Dismiss"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.suggestionDecided(row.sid, true)
      }
      Button {
        visible: row.isLive && !row.hasSuggestion
        text: "a Add"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.addOpenRequested(row.sid)
      }
      Button {
        visible: row.hasPreview
        text: "p Preview"
        bordered: true
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.previewRequested(row.sid)
      }
      Button {
        visible: row.isLive || row.isOrphaned
        text: "z Pause"
        bordered: true
        enabled: !row.stopping && row.busyText === ""
        foreground: row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.pauseRequested(row.sid)
      }
      Button {
        visible: row.isLive || row.isOrphaned || row.isPaused
        text: row.stopLabel()
        bordered: true
        enabled: !row.stopping
        foreground: (row.stopArmed || row.stopping) ? row.urgentColor : row.foreground
        fontFamily: row.fontFamily
        fontSize: Style.font.caption
        onClicked: row.stopArmed ? row.stopConfirmRequested(row.sid) : row.stopArmRequested(row.sid)
      }
    }
  }

  PanelToolTip {
    visible: rowMouse.containsMouse && !row.hasCursor && row.branch !== ""
    text: row.branch
    fontFamily: row.fontFamily
  }
}
