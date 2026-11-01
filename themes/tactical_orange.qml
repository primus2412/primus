import QtQuick
import QtQuick.Controls
import QtQuick.Window

Window {
    id: mainWindow
    visible: true
    width: Screen.width
    height: Screen.height
    flags: Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
    color: "transparent"

    property string consolePlaceholder: "Type command here..."
    property bool overlayAcceptsInput: true

    function setClickThrough(enabled) {
        var base = Qt.Window | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        mainWindow.flags = enabled ? (base | Qt.WindowTransparentForInput) : base
        if (!enabled) {
            mainWindow.requestActivate()
            consoleInputField.forceActiveFocus()
        }
    }

    Component.onCompleted: {
        setClickThrough(false)
    }

    // ── Python Bridge Connections ──────────────────────────────────────────
    Connections {
        target: pyBridge

        // Confirmation prompt
        function onShowPrompt(risk, agent, action, targetStr, command) {
            riskLevelText.text = "⚠  " + risk + " RISK DETECTED  ⚠"
            promptDetailsText.text = "Agent: " + agent + "\nAction: " + action + "\nTarget: " + targetStr
            commandPreviewText.text = "> " + command
            confirmationPanel.visible = true
            mainWindow.setClickThrough(false)
        }
        function onHidePrompt() {
            confirmationPanel.visible = false
            if (!enrollmentWizard.visible && !passwordPanel.visible && overlayAcceptsInput)
                mainWindow.setClickThrough(false)
        }

        // Password fallback
        function onShowPasswordPrompt(errorMsg) {
            passwordErrorText.text = errorMsg
            passwordPanel.visible = true
            passwordInputField.focus = true
            mainWindow.setClickThrough(false)
        }
        function onHidePasswordPrompt() {
            passwordPanel.visible = false
            lockoutTimer.stop()
            submitPassBtn.enabled = true
            if (!confirmationPanel.visible && !enrollmentWizard.visible && overlayAcceptsInput)
                mainWindow.setClickThrough(false)
        }
        function onLockoutStarted(seconds) {
            lockoutTimer.secondsRemaining = seconds
            lockoutTimer.start()
            submitPassBtn.enabled = false
        }

        // Enrollment wizard
        function onShowEnrollment() {
            enrollmentWizard.visible = true
            enrollmentWizard.currentStep = 0
            mainWindow.setClickThrough(false)
        }
        function onHideEnrollment() {
            enrollmentWizard.visible = false
            if (!passwordPanel.visible && !confirmationPanel.visible && overlayAcceptsInput)
                mainWindow.setClickThrough(false)
        }
        function onEnrollmentStepUpdate(step, total, phrase, statusMsg) {
            enrollmentWizard.currentStep = step
            enrollmentWizard.totalSteps = total
            enrollmentWizard.currentPhrase = phrase
            enrollmentWizard.statusMessage = statusMsg
        }
        function onEnrollmentRecording(isRecordingVal) {
            enrollmentWizard.isRecording = isRecordingVal
        }
        function onEnrollmentComplete(success) {
            enrollmentWizard.enrollSuccess = success
            enrollmentWizard.currentStep = enrollmentWizard.totalSteps  // show completion page
        }

        // HUD telemetry updates
        function onTelemetryUpdate(cpu, ram, gpu) {
            telemetryText.text = "CPU  " + cpu + "%\nRAM  " + ram + "%\nGPU  " + gpu + "%"
        }
        
        function onStatusUpdate(msg) {
            statusText.text = msg
            // Dynamically update placeholder based on microphone status
            if (msg.indexOf("MIC UNAVAILABLE") !== -1) {
                consolePlaceholder = "MIC UNAVAILABLE - Enter command here..."
            } else if (msg.indexOf("LISTENING") !== -1) {
                consolePlaceholder = "SYSTEM: LISTENING... (or type command here)"
            } else if (msg.indexOf("STANDBY") !== -1) {
                consolePlaceholder = "SYSTEM: STANDBY - Press hotkey to listen or type here..."
            } else {
                consolePlaceholder = "Type command here..."
            }
        }
        
        function onOcrUnavailableNotice() {
            ocrNotice.visible = true
        }
        
        function onOverlayVisibilityChanged(visibleVal) {
            mainWindow.visible = visibleVal
            overlayAcceptsInput = visibleVal
            mainWindow.setClickThrough(!visibleVal)
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // HUD  — always-on-top telemetry panel (top-right corner)
    // ═══════════════════════════════════════════════════════════════════════
    Rectangle {
        id: hudContainer
        width: 380
        height: 220
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: 48
        color: Qt.rgba(0.04, 0.04, 0.04, 0.85)
        border.color: "#FF6600"
        border.width: 2
        radius: 10

        // Outer glow
        Rectangle {
            anchors.fill: parent; anchors.margins: -6
            color: "transparent"
            border.color: Qt.rgba(1.0, 0.4, 0.0, 0.25)
            border.width: 6; radius: 16; z: -1
        }

        // Scan-line animation
        Rectangle {
            id: scanLine
            width: parent.width - 4; height: 2
            anchors.horizontalCenter: parent.horizontalCenter
            color: Qt.rgba(1.0, 0.5, 0.0, 0.15)
            y: 0
            NumberAnimation on y {
                from: 0; to: hudContainer.height
                duration: 3000; loops: Animation.Infinite
            }
        }

        Column {
            anchors.fill: parent
            anchors.margins: 18
            spacing: 12

            Row {
                spacing: 10
                Rectangle { width: 8; height: 8; radius: 4; color: "#FF6600"
                    SequentialAnimation on opacity { loops: Animation.Infinite
                        NumberAnimation { to: 0.3; duration: 900 }
                        NumberAnimation { to: 1.0; duration: 900 } } }
                Text {
                    text: "PRIMUS  AI  SYSTEM"
                    color: "#FF8800"; font.pixelSize: 20; font.bold: true; font.family: "Courier New"
                }
            }

            Rectangle { width: parent.width; height: 1; color: "#FF6600"; opacity: 0.6 }

            Text {
                id: statusText
                text: "SYSTEM: ONLINE\nMODE: ASSISTED\nTHREAT: NONE"
                color: "#FFCC88"; font.pixelSize: 13; font.family: "Courier New"; lineHeight: 1.6
            }

            Text {
                id: telemetryText
                text: "CPU  --%\nRAM  --%\nGPU  --%"
                color: "#FF9900"; font.pixelSize: 13; font.family: "Courier New"; lineHeight: 1.6
            }
        }

        // OCR unavailable notice strip
        Rectangle {
            id: ocrNotice
            visible: false
            width: parent.width; height: 24
            anchors.bottom: parent.bottom; radius: 10
            color: Qt.rgba(0.4, 0.2, 0.0, 0.9)
            Text {
                anchors.centerIn: parent
                text: "⚠  OCR unavailable — screenshot-only mode"
                color: "#FFCC44"; font.pixelSize: 11; font.family: "Courier New"
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // VOICE ENROLLMENT WIZARD (cinematic, fullscreen overlay)
    // ═══════════════════════════════════════════════════════════════════════
    Rectangle {
        id: enrollmentWizard
        visible: false
        anchors.fill: parent
        color: Qt.rgba(0.0, 0.0, 0.0, 0.92)

        property int  currentStep:   0
        property int  totalSteps:    2
        property string currentPhrase: "Speak naturally for PRIMUS."
        property string statusMessage: "Initializing..."
        property bool   enrollSuccess: false
        property bool   isRecording:   false

        // Corner grid decoration
        Repeater {
            model: 4
            Rectangle {
                width: 60; height: 60
                color: "transparent"
                border.color: Qt.rgba(1.0, 0.4, 0.0, 0.3); border.width: 2
                x: index % 2 === 0 ? 0 : enrollmentWizard.width - width
                y: index < 2     ? 0 : enrollmentWizard.height - height
            }
        }

        Column {
            anchors.centerIn: parent
            spacing: 24
            width: 640

            // Title
            Text {
                text: "PRIMUS  INITIAL  SETUP"
                color: "#FF8800"; font.pixelSize: 32; font.bold: true; font.family: "Courier New"
                anchors.horizontalCenter: parent.horizontalCenter
            }

            Rectangle { width: parent.width; height: 1; color: "#FF6600" }

            // Step indicator
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 16
                Repeater {
                    model: enrollmentWizard.totalSteps
                    Rectangle {
                        width: 28; height: 28; radius: 14
                        color: index < enrollmentWizard.currentStep ? "#FF6600"
                             : index === enrollmentWizard.currentStep ? "#FF8800"
                             : Qt.rgba(1.0, 0.4, 0.0, 0.2)
                        border.color: "#FF6600"; border.width: 1
                        Text {
                            anchors.centerIn: parent; text: (index + 1)
                            visible: index > 0
                            color: "white"; font.pixelSize: 12; font.bold: true; font.family: "Courier New"
                        }
                        Text {
                            anchors.centerIn: parent; text: "🔑"
                            visible: index === 0
                            font.pixelSize: 12
                        }
                    }
                }
            }

            // Dynamic page switcher
            Loader {
                width: parent.width
                anchors.horizontalCenter: parent.horizontalCenter

                sourceComponent: enrollmentWizard.currentStep === 0 ? welcomePage
                               : enrollmentWizard.currentStep < enrollmentWizard.totalSteps ? recordPage
                               : completionPage
            }

            Component {
                id: welcomePage
                Column {
                    spacing: 16; width: 640
                    Text {
                        text: "MASTER  PASSWORD  SETUP"
                        color: "#FF8800"; font.pixelSize: 22; font.bold: true; font.family: "Courier New"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        width: parent.width; wrapMode: Text.WordWrap
                        text: "Create a master password to unlock the encrypted vault and authorize commands.\n\n" +
                              "You will be prompted for this password when running shell commands or high-risk actions."
                        color: "#FFAA66"; font.pixelSize: 14; font.family: "Courier New"; lineHeight: 1.5
                        horizontalAlignment: Text.AlignHCenter
                    }
                    Text {
                        id: welcomeErrorText
                        text: ""
                        visible: text.length > 0
                        color: "#FF4444"; font.pixelSize: 12; font.family: "Courier New"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    TextField {
                        id: setupPasswordField
                        width: 320; height: 40
                        anchors.horizontalCenter: parent.horizontalCenter
                        placeholderText: "Enter Master Password (min 8 chars)"
                        echoMode: TextInput.Password
                        color: "white"
                        placeholderTextColor: "#777"
                        background: Rectangle {
                            color: "#181818"
                            border.color: setupPasswordField.activeFocus ? "#FF6600" : "#444"
                            border.width: 1; radius: 4
                        }
                        font.pixelSize: 14; font.family: "Courier New"
                        horizontalAlignment: TextInput.AlignHCenter
                    }
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 220; height: 45; radius: 6
                        color: beginMouseArea.pressed ? "#CC4400" : "#FF6600"
                        Text {
                            anchors.centerIn: parent; text: "[ CREATE ACCOUNT ]"
                            color: "white"; font.pixelSize: 15; font.bold: true; font.family: "Courier New"
                        }
                        MouseArea {
                            id: beginMouseArea; anchors.fill: parent
                            onClicked: {
                                if (setupPasswordField.text.length < 8) {
                                    welcomeErrorText.text = "✕ Password is too short! (Min 8 chars)"
                                } else {
                                    welcomeErrorText.text = ""
                                    pyBridge.onEnrollmentBegin(setupPasswordField.text)
                                    setupPasswordField.text = ""
                                }
                            }
                        }
                    }
                }
            }

            Component {
                id: recordPage
                Column {
                    spacing: 24; width: 640
                    Text {
                        text: enrollmentWizard.currentStep === 1 ? "MICROPHONE CALIBRATION" : "SAMPLE  " + (enrollmentWizard.currentStep - 1) + "  OF  " + (enrollmentWizard.totalSteps - 1)
                        color: "#FF8800"; font.pixelSize: 18; font.bold: true; font.family: "Courier New"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }

                    // Phrase display
                    Rectangle {
                        width: parent.width; height: 80; radius: 8
                        color: Qt.rgba(1.0, 0.4, 0.0, 0.08); border.color: "#FF6600"; border.width: 1
                        Text {
                            anchors.centerIn: parent; width: parent.width - 32
                            text: enrollmentWizard.currentStep === 1 ? enrollmentWizard.currentPhrase : '"' + enrollmentWizard.currentPhrase + '"'
                            color: "#FFFFEE"; font.pixelSize: 18; font.italic: true; font.family: "Courier New"
                            wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
                        }
                    }

                    // Animated microphone ring
                    Item {
                        id: micPulse
                        property bool isActive: enrollmentWizard.isRecording
                        width: 120; height: 120
                        anchors.horizontalCenter: parent.horizontalCenter

                        // Outer pulse ring
                        Rectangle {
                            id: pulseRing
                            anchors.centerIn: parent
                            width: 100; height: 100; radius: 50
                            color: "transparent"
                            border.color: micPulse.isActive ? "#FF6600" : Qt.rgba(1.0, 0.4, 0.0, 0.3)
                            border.width: 3
                            SequentialAnimation on scale {
                                running: micPulse.isActive; loops: Animation.Infinite
                                NumberAnimation { to: 1.25; duration: 600; easing.type: Easing.OutQuad }
                                NumberAnimation { to: 1.0;  duration: 600; easing.type: Easing.InQuad }
                            }
                            SequentialAnimation on opacity {
                                running: micPulse.isActive; loops: Animation.Infinite
                                NumberAnimation { to: 0.4; duration: 600 }
                                NumberAnimation { to: 1.0; duration: 600 }
                            }
                        }

                        // Mic icon
                        Rectangle {
                            anchors.centerIn: parent; width: 60; height: 60; radius: 30
                            color: micPulse.isActive ? "#FF6600" : Qt.rgba(1.0, 0.4, 0.0, 0.3)
                            Text {
                                anchors.centerIn: parent; text: "🎤"
                                font.pixelSize: 26
                            }
                        }
                    }

                    Text {
                        text: enrollmentWizard.statusMessage
                        color: "#FFCC88"; font.pixelSize: 14; font.family: "Courier New"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }

                    // Record / Retry button
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 220; height: 50; radius: 6
                        
                        visible: enrollmentWizard.currentStep > 1 || (enrollmentWizard.currentStep === 1 && enrollmentWizard.currentPhrase.indexOf("FAILED") !== -1)
                        
                        color: micPulse.isActive ? "#AA2200" : (recMouseArea.pressed ? "#CC4400" : "#FF6600")
                        Text {
                            anchors.centerIn: parent
                            text: enrollmentWizard.currentStep === 1 ? "[ RETRY CALIBRATION ]" : (micPulse.isActive ? "[ RECORDING... ]" : "[ RECORD SAMPLE ]")
                            color: "white"; font.pixelSize: 15; font.bold: true; font.family: "Courier New"
                        }
                        MouseArea {
                            id: recMouseArea; anchors.fill: parent
                            onClicked: {
                                if (enrollmentWizard.currentStep === 1) {
                                    pyBridge.onEnrollmentRetry()
                                } else {
                                    pyBridge.onEnrollmentRecord()
                                }
                            }
                        }
                    }
                }
            }

            Component {
                id: completionPage
                Column {
                    spacing: 24; width: 640
                    Text {
                        text: enrollmentWizard.enrollSuccess ? "✓  ENROLLMENT  COMPLETE" : "✕  ENROLLMENT  FAILED"
                        color: enrollmentWizard.enrollSuccess ? "#00FF88" : "#FF4444"
                        font.pixelSize: 28; font.bold: true; font.family: "Courier New"
                        anchors.horizontalCenter: parent.horizontalCenter
                    }
                    Text {
                        anchors.horizontalCenter: parent.horizontalCenter; width: parent.width
                        text: enrollmentWizard.enrollSuccess
                            ? "Your master password and encrypted vault are ready.\n\nPRIMUS is ready — use the command bar or voice hotkey."
                            : "Password setup failed.\nPlease try again with a password of at least 8 characters."
                        color: "#FFCC88"; font.pixelSize: 15; font.family: "Courier New"
                        lineHeight: 1.6; wrapMode: Text.WordWrap; horizontalAlignment: Text.AlignHCenter
                    }
                    Rectangle {
                        anchors.horizontalCenter: parent.horizontalCenter
                        width: 180; height: 50; radius: 6
                        color: closeMouseArea.pressed ? "#CC4400" : "#FF6600"
                        Text {
                            anchors.centerIn: parent; text: "[ CLOSE ]"
                            color: "white"; font.pixelSize: 16; font.bold: true; font.family: "Courier New"
                        }
                        MouseArea {
                            id: closeMouseArea; anchors.fill: parent
                            onClicked: pyBridge.onEnrollmentClose()
                        }
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // CONFIRMATION PANEL (Phase 2 — command gating)
    // ═══════════════════════════════════════════════════════════════════════
    Rectangle {
        id: confirmationPanel
        visible: false
        width: 620; height: 420
        anchors.centerIn: parent
        color: Qt.rgba(0.1, 0.0, 0.0, 0.96)
        border.color: "#FF0000"; border.width: 3; radius: 15

        Rectangle {
            anchors.fill: parent; anchors.margins: -10
            color: "transparent"
            border.color: Qt.rgba(1.0, 0.0, 0.0, 0.45); border.width: 10; radius: 25; z: -1
        }

        Column {
            anchors.fill: parent; anchors.margins: 30; spacing: 16

            Text {
                id: riskLevelText
                text: "⚠  CRITICAL RISK DETECTED  ⚠"
                color: "#FF0000"; font.pixelSize: 26; font.bold: true; font.family: "Courier New"
                anchors.horizontalCenter: parent.horizontalCenter
            }

            Rectangle { width: parent.width; height: 2; color: "#FF0000" }

            Text {
                id: promptDetailsText
                text: "Agent: —\nAction: —\nTarget: —"
                color: "#FFCCCC"; font.pixelSize: 15; font.family: "Courier New"
                lineHeight: 1.5; wrapMode: Text.WordWrap; width: parent.width
            }

            Rectangle {
                width: parent.width; height: 90; color: "#220000"; radius: 6
                border.color: "#FF3333"; border.width: 1
                Text {
                    id: commandPreviewText
                    anchors.fill: parent; anchors.margins: 10
                    text: "> —"; color: "#FFAAAA"; font.pixelSize: 14; font.family: "Consolas"
                    wrapMode: Text.WrapAnywhere
                }
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter; spacing: 40; topPadding: 10

                Rectangle {
                    width: 150; height: 50; radius: 6
                    color: denyArea.pressed ? "#550000" : "#AA0000"
                    border.color: "#FF0000"; border.width: 2
                    Text { anchors.centerIn: parent; text: "[ DENY ]"
                           color: "white"; font.pixelSize: 18; font.bold: true; font.family: "Courier New" }
                    MouseArea { id: denyArea; anchors.fill: parent; onClicked: pyBridge.onPromptResponse(false) }
                }

                Rectangle {
                    width: 150; height: 50; radius: 6
                    color: approveArea.pressed ? "#002200" : "#004400"
                    border.color: "#00FF00"; border.width: 2
                    Text { anchors.centerIn: parent; text: "[ APPROVE ]"
                           color: "#AAFFAA"; font.pixelSize: 18; font.bold: true; font.family: "Courier New" }
                    MouseArea { id: approveArea; anchors.fill: parent; onClicked: pyBridge.onPromptResponse(true) }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // PASSWORD PANEL (Section 14 — fallback manual verification)
    // ═══════════════════════════════════════════════════════════════════════
    Rectangle {
        id: passwordPanel
        visible: false
        width: 520; height: 320
        anchors.centerIn: parent
        color: Qt.rgba(0.08, 0.08, 0.08, 0.96)
        border.color: "#FF6600"; border.width: 2; radius: 10

        Rectangle {
            anchors.fill: parent; anchors.margins: -8
            color: "transparent"
            border.color: Qt.rgba(1.0, 0.4, 0.0, 0.25); border.width: 8; radius: 18; z: -1
        }

        Timer {
            id: lockoutTimer
            interval: 1000; running: false; repeat: true
            property int secondsRemaining: 0
            onTriggered: {
                if (secondsRemaining > 0) {
                    secondsRemaining--
                    passwordErrorText.text = "TOO MANY ATTEMPTS: Locked out for " + secondsRemaining + "s."
                } else {
                    lockoutTimer.stop()
                    passwordErrorText.text = "Lockout ended. Enter password to unlock."
                    submitPassBtn.enabled = true
                }
            }
        }

        Column {
            anchors.fill: parent; anchors.margins: 30; spacing: 20

            Text {
                text: "🔒 MASTER PASSWORD REQUIREMENT"
                color: "#FF8800"; font.pixelSize: 20; font.bold: true; font.family: "Courier New"
                anchors.horizontalCenter: parent.horizontalCenter
            }

            Rectangle { width: parent.width; height: 1; color: "#FF6600"; opacity: 0.6 }

            Text {
                id: passwordErrorText
                text: "Fallback verification required. Please enter master password."
                color: "#FFCC88"; font.pixelSize: 13; font.family: "Courier New"
                wrapMode: Text.WordWrap; width: parent.width; horizontalAlignment: Text.AlignHCenter
            }

            TextField {
                id: passwordInputField
                width: parent.width; height: 42
                placeholderText: "Enter master password"
                echoMode: TextInput.Password
                color: "white"
                placeholderTextColor: "#666"
                background: Rectangle {
                    color: "#181818"
                    border.color: passwordInputField.activeFocus ? "#FF6600" : "#444"
                    border.width: 1; radius: 4
                }
                font.pixelSize: 15; font.family: "Courier New"
                horizontalAlignment: TextInput.AlignHCenter
                Keys.onReturnPressed: {
                    if (submitPassBtn.enabled) {
                        pyBridge.onPasswordSubmit(passwordInputField.text)
                        passwordInputField.text = ""
                    }
                }
                Keys.onEscapePressed: {
                    pyBridge.onPasswordCancel()
                    passwordInputField.text = ""
                }
            }

            Row {
                anchors.horizontalCenter: parent.horizontalCenter; spacing: 30

                Rectangle {
                    width: 130; height: 40; radius: 6
                    color: cancelPassArea.pressed ? "#552200" : "#883300"
                    Text { anchors.centerIn: parent; text: "[ CANCEL ]"; color: "white"; font.bold: true; font.family: "Courier New" }
                    MouseArea {
                        id: cancelPassArea; anchors.fill: parent
                        onClicked: {
                            pyBridge.onPasswordCancel()
                            passwordInputField.text = ""
                        }
                    }
                }

                Rectangle {
                    id: submitPassBtn
                    width: 130; height: 40; radius: 6
                    property bool enabled: true
                    color: enabled ? (submitPassArea.pressed ? "#004400" : "#007700") : "#333"
                    Text { anchors.centerIn: parent; text: "[ SUBMIT ]"; color: submitPassBtn.enabled ? "#AAFFAA" : "#888"; font.bold: true; font.family: "Courier New" }
                    MouseArea {
                        id: submitPassArea; anchors.fill: parent
                        onClicked: {
                            if (submitPassBtn.enabled) {
                                pyBridge.onPasswordSubmit(passwordInputField.text)
                                passwordInputField.text = ""
                            }
                        }
                    }
                }
            }
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // CONSOLE INPUT PANEL (Degraded Mode Keyboard Prompt)
    // ═══════════════════════════════════════════════════════════════════════
    Rectangle {
        id: consoleInputPanel
        width: 720; height: 50
        anchors.bottom: parent.bottom
        anchors.horizontalCenter: parent.horizontalCenter
        anchors.bottomMargin: 40
        color: Qt.rgba(0.04, 0.04, 0.04, 0.9)
        border.color: "#FF6600"
        border.width: 1
        radius: 6
        visible: mainWindow.visible
        z: 200

        Rectangle {
            anchors.fill: parent; anchors.margins: -4
            color: "transparent"
            border.color: Qt.rgba(1.0, 0.4, 0.0, 0.15); border.width: 4; radius: 10; z: -1
        }

        Row {
            anchors.fill: parent; anchors.margins: 10; spacing: 10
            Text {
                text: ">"
                color: "#FF8800"
                font.pixelSize: 18; font.bold: true; font.family: "Courier New"
                anchors.verticalCenter: parent.verticalCenter
            }
            TextField {
                id: consoleInputField
                width: parent.width - 40; height: 30
                anchors.verticalCenter: parent.verticalCenter
                placeholderText: consolePlaceholder
                color: "#FFFFEE"
                font.pixelSize: 14; font.family: "Consolas"
                placeholderTextColor: "#885522"
                focus: true
                activeFocusOnPress: true
                selectByMouse: true
                background: Rectangle { color: "transparent" }
                
                Keys.onReturnPressed: {
                    if (text.trim() !== "") {
                        pyBridge.onTextCommandSubmit(text)
                        text = ""
                    }
                }
            }
        }
    }
}
