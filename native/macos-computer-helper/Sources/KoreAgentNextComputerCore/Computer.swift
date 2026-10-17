import AppKit
import ApplicationServices
import Foundation

public final class ComputerController {
    private var sessionID: String?
    private var observationID: String?
    private var elements: [String: AXUIElement] = [:]
    private var windows: [String: AXUIElement] = [:]
    private var targetPID: pid_t?

    public init() {}

    public func handle(_ request: HelperRequest) throws -> JSONValue {
        switch request.method {
        case "status": return status()
        case "request_permission": return requestPermission(request.params)
        case "begin_session": return try beginSession(request.params)
        case "end_session": return try endSession(request.params)
        case "observe": try requireSession(request.params); return try observe(request.params)
        case "click": try requireSession(request.params); return try click(request.params)
        case "type": try requireSession(request.params); return try typeText(request.params)
        case "key": try requireSession(request.params); return try key(request.params)
        case "scroll": try requireSession(request.params); return try scroll(request.params)
        case "open_app": try requireSession(request.params); return try openApp(request.params)
        case "focus_app": try requireSession(request.params); return try focusApp(request.params)
        case "focus_window": try requireSession(request.params); return try focusWindow(request.params)
        default: throw HelperError("method_not_found", "Unknown method: \(request.method)")
        }
    }

    private func status() -> JSONValue {
        .object([
            "available": .bool(true),
            "platform": .string("macos"),
            "session_id": sessionID.map(JSONValue.string) ?? .null,
            "permissions": .object([
                "accessibility": .string(AXIsProcessTrusted() ? "granted" : "required"),
                "screen_recording": .string(CGPreflightScreenCaptureAccess() ? "granted" : "required"),
            ]),
        ])
    }

    private func requestPermission(_ params: [String: JSONValue]) -> JSONValue {
        if params["permission"]?.string == "screen_recording" {
            _ = CGRequestScreenCaptureAccess()
        } else {
            let options = [kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String: true] as CFDictionary
            _ = AXIsProcessTrustedWithOptions(options)
        }
        return status()
    }

    private func beginSession(_ params: [String: JSONValue]) throws -> JSONValue {
        guard let incoming = params["session_id"]?.string, !incoming.isEmpty else {
            throw HelperError("invalid_params", "session_id is required")
        }
        if let current = sessionID, current != incoming {
            throw HelperError("session_busy", "another computer session is active")
        }
        sessionID = incoming
        invalidateSnapshot()
        return .object(["ok": .bool(true), "session_id": .string(incoming)])
    }

    private func endSession(_ params: [String: JSONValue]) throws -> JSONValue {
        try requireSession(params)
        sessionID = nil
        invalidateSnapshot()
        return .object(["ok": .bool(true)])
    }

    private func requireSession(_ params: [String: JSONValue]) throws {
        guard let current = sessionID else {
            throw HelperError("session_not_active", "begin a computer session first")
        }
        guard params["session_id"]?.string == current else {
            throw HelperError("session_mismatch", "request does not belong to the active session")
        }
    }

    private func requireObservation(_ params: [String: JSONValue]) throws {
        guard let current = observationID,
              params["observation_id"]?.string == current else {
            throw HelperError("stale_observation", "observe again before acting")
        }
    }

    private func invalidateSnapshot() {
        observationID = nil
        elements.removeAll()
        windows.removeAll()
        targetPID = nil
    }

    private func observe(_ params: [String: JSONValue]) throws -> JSONValue {
        guard let running = NSWorkspace.shared.frontmostApplication else {
            throw HelperError("target_not_running", "no frontmost application")
        }
        let root = AXUIElementCreateApplication(running.processIdentifier)
        targetPID = running.processIdentifier
        let observedID = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        observationID = observedID
        elements.removeAll()
        windows.removeAll()

        var windowValues: [JSONValue] = []
        for (index, window) in attributeElements(root, kAXWindowsAttribute as String).prefix(50).enumerated() {
            let ref = "w\(index + 1)"
            windows[ref] = window
            windowValues.append(.object([
                "ref": .string(ref),
                "title": .string(attributeString(window, kAXTitleAttribute as String) ?? ""),
                "role": .string(attributeString(window, kAXRoleAttribute as String) ?? "AXWindow"),
                "bounds": frameValue(window),
            ]))
        }

        var elementValues: [JSONValue] = []
        collectElements(root, depth: 0, limit: 300, output: &elementValues)

        var screenshot: JSONValue = .null
        if let raw = params["screenshot_path"]?.string {
            let path = URL(fileURLWithPath: raw).standardizedFileURL.path
            let process = Process()
            process.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            process.arguments = ["-x", path]
            try process.run()
            process.waitUntilExit()
            if process.terminationStatus == 0 { screenshot = .string(path) }
        }
        return .object([
            "observation_id": .string(observedID),
            "active_app": .object([
                "name": .string(running.localizedName ?? ""),
                "bundle_id": running.bundleIdentifier.map(JSONValue.string) ?? .null,
                "pid": .number(Double(running.processIdentifier)),
            ]),
            "windows": .array(windowValues),
            "elements": .array(elementValues),
            "screenshot_path": screenshot,
        ])
    }

    private func collectElements(_ element: AXUIElement, depth: Int, limit: Int, output: inout [JSONValue]) {
        if depth > 7 || output.count >= limit { return }
        let role = attributeString(element, kAXRoleAttribute as String) ?? ""
        if !role.isEmpty && role != (kAXApplicationRole as String) {
            let ref = "e\(output.count + 1)"
            elements[ref] = element
            output.append(.object([
                "ref": .string(ref),
                "role": .string(role),
                "title": .string(attributeString(element, kAXTitleAttribute as String) ?? ""),
                "value": .string(attributeString(element, kAXValueAttribute as String) ?? ""),
                "enabled": .bool(attributeBool(element, kAXEnabledAttribute as String) ?? true),
                "focused": .bool(attributeBool(element, kAXFocusedAttribute as String) ?? false),
                "editable": .bool(role == (kAXTextFieldRole as String) || role == (kAXTextAreaRole as String)),
                "actions": .array(copyActions(element).map(JSONValue.string)),
                "bounds": frameValue(element),
            ]))
        }
        for child in attributeElements(element, kAXChildrenAttribute as String) {
            collectElements(child, depth: depth + 1, limit: limit, output: &output)
            if output.count >= limit { break }
        }
    }

    private func click(_ params: [String: JSONValue]) throws -> JSONValue {
        guard AXIsProcessTrusted() else { throw HelperError("permission_required", "Accessibility permission is required") }
        try requireObservation(params)
        if let ref = params["element_ref"]?.string {
            guard let element = elements[ref] else { throw HelperError("element_not_found", "element reference is stale") }
            if AXUIElementPerformAction(element, kAXPressAction as CFString) == .success {
                invalidateSnapshot()
                return delivered(["element_ref": .string(ref)])
            }
            if let point = centerPoint(element) {
                postClick(point)
                invalidateSnapshot()
                return delivered(["element_ref": .string(ref)])
            }
            throw HelperError("action_not_supported", "element cannot be clicked")
        }
        guard let x = params["x"]?.number, let y = params["y"]?.number else {
            throw HelperError("invalid_params", "element_ref or x/y is required")
        }
        postClick(CGPoint(x: x, y: y))
        invalidateSnapshot()
        return delivered()
    }

    private func postClick(_ point: CGPoint) {
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseDown, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
        CGEvent(mouseEventSource: nil, mouseType: .leftMouseUp, mouseCursorPosition: point, mouseButton: .left)?.post(tap: .cghidEventTap)
    }

    private func typeText(_ params: [String: JSONValue]) throws -> JSONValue {
        guard AXIsProcessTrusted() else { throw HelperError("permission_required", "Accessibility permission is required") }
        try requireObservation(params)
        try restoreObservedTarget()
        guard let text = params["text"]?.string else { throw HelperError("invalid_params", "text is required") }
        if let ref = params["element_ref"]?.string {
            guard let element = elements[ref] else { throw HelperError("element_not_found", "element reference is stale") }
            let role = attributeString(element, kAXRoleAttribute as String) ?? ""
            guard role == (kAXTextFieldRole as String) || role == (kAXTextAreaRole as String) else {
                throw HelperError("not_editable", "type requires an editable element")
            }
            _ = AXUIElementSetAttributeValue(element, kAXFocusedAttribute as CFString, kCFBooleanTrue)
        }
        let event = CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: true)
        var chars = Array(text.utf16)
        event?.keyboardSetUnicodeString(stringLength: chars.count, unicodeString: &chars)
        event?.post(tap: .cghidEventTap)
        CGEvent(keyboardEventSource: nil, virtualKey: 0, keyDown: false)?.post(tap: .cghidEventTap)
        invalidateSnapshot()
        return delivered(["characters": .number(Double(text.count))])
    }

    private func key(_ params: [String: JSONValue]) throws -> JSONValue {
        try requireObservation(params)
        try restoreObservedTarget()
        guard let name = params["key"]?.string else { throw HelperError("invalid_params", "key is required") }
        let codes: [String: CGKeyCode] = ["return": 36, "tab": 48, "space": 49, "escape": 53, "left": 123, "right": 124, "down": 125, "up": 126, "delete": 51]
        guard let code = codes[name.lowercased()] else { throw HelperError("invalid_params", "unsupported key") }
        let flags = modifierFlags(params["modifiers"]?.array ?? [])
        let down = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: true)
        let up = CGEvent(keyboardEventSource: nil, virtualKey: code, keyDown: false)
        down?.flags = flags
        up?.flags = flags
        down?.post(tap: .cghidEventTap)
        up?.post(tap: .cghidEventTap)
        invalidateSnapshot()
        return delivered(["key": .string(name.lowercased())])
    }

    private func scroll(_ params: [String: JSONValue]) throws -> JSONValue {
        try requireObservation(params)
        let dy = Int32(params["delta_y"]?.number ?? 0)
        let dx = Int32(params["delta_x"]?.number ?? 0)
        CGEvent(scrollWheelEvent2Source: nil, units: .pixel, wheelCount: 2, wheel1: dy, wheel2: dx, wheel3: 0)?.post(tap: .cghidEventTap)
        invalidateSnapshot()
        return delivered()
    }

    private func openApp(_ params: [String: JSONValue]) throws -> JSONValue {
        guard let name = params["app"]?.string else { throw HelperError("invalid_params", "app is required") }
        if let existing = NSWorkspace.shared.runningApplications.first(where: { $0.localizedName == name || $0.bundleIdentifier == name }) {
            existing.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
            invalidateSnapshot()
            return delivered(["pid": .number(Double(existing.processIdentifier))])
        }
        guard NSWorkspace.shared.launchApplication(name) else { throw HelperError("not_found", "application could not be launched") }
        invalidateSnapshot()
        return delivered()
    }

    private func focusApp(_ params: [String: JSONValue]) throws -> JSONValue {
        guard let name = params["app"]?.string else { throw HelperError("invalid_params", "app is required") }
        guard let app = NSWorkspace.shared.runningApplications.first(where: { $0.localizedName == name || $0.bundleIdentifier == name }) else {
            throw HelperError("not_found", "application not found")
        }
        app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        invalidateSnapshot()
        return delivered(["pid": .number(Double(app.processIdentifier))])
    }

    private func focusWindow(_ params: [String: JSONValue]) throws -> JSONValue {
        try requireObservation(params)
        guard let ref = params["window_ref"]?.string, let window = windows[ref] else {
            throw HelperError("target_window_not_found", "window reference is stale")
        }
        let main = AXUIElementSetAttributeValue(window, kAXMainAttribute as CFString, kCFBooleanTrue)
        let raised = AXUIElementPerformAction(window, kAXRaiseAction as CFString)
        guard main == .success || raised == .success else { throw HelperError("focus_failed", "window could not be focused") }
        invalidateSnapshot()
        return delivered(["window_ref": .string(ref)])
    }

    private func restoreObservedTarget() throws {
        guard let pid = targetPID,
              let app = NSRunningApplication(processIdentifier: pid) else {
            throw HelperError("target_not_running", "observed application is no longer running")
        }
        if NSWorkspace.shared.frontmostApplication?.processIdentifier != pid {
            app.activate(options: [.activateAllWindows, .activateIgnoringOtherApps])
        }
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == pid else {
            throw HelperError("focus_failed", "could not restore the observed application")
        }
    }

    private func delivered(_ values: [String: JSONValue] = [:]) -> JSONValue {
        var result = values
        result["ok"] = .bool(true)
        result["delivery_status"] = .string("delivered")
        result["verification_status"] = .string("pending")
        result["requires_fresh_observation"] = .bool(true)
        return .object(result)
    }

    private func modifierFlags(_ values: [JSONValue]) -> CGEventFlags {
        var flags: CGEventFlags = []
        for value in values.compactMap(\.string).map({ $0.lowercased() }) {
            switch value {
            case "command", "cmd": flags.insert(.maskCommand)
            case "shift": flags.insert(.maskShift)
            case "option", "alt": flags.insert(.maskAlternate)
            case "control", "ctrl": flags.insert(.maskControl)
            default: continue
            }
        }
        return flags
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var value: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &value) == .success ? value : nil
    }

    private func attributeString(_ element: AXUIElement, _ name: String) -> String? {
        guard let value = attribute(element, name) else { return nil }
        if CFGetTypeID(value) == CFStringGetTypeID() { return value as? String }
        if CFGetTypeID(value) == CFNumberGetTypeID() { return String(describing: value) }
        return nil
    }

    private func attributeBool(_ element: AXUIElement, _ name: String) -> Bool? { attribute(element, name) as? Bool }
    private func attributeElements(_ element: AXUIElement, _ name: String) -> [AXUIElement] { attribute(element, name) as? [AXUIElement] ?? [] }

    private func copyActions(_ element: AXUIElement) -> [String] {
        var names: CFArray?
        guard AXUIElementCopyActionNames(element, &names) == .success else { return [] }
        return names as? [String] ?? []
    }

    private func frameValue(_ element: AXUIElement) -> JSONValue {
        guard let frame = frame(element) else { return .null }
        return .object(["x": .number(frame.origin.x), "y": .number(frame.origin.y), "width": .number(frame.width), "height": .number(frame.height)])
    }

    private func centerPoint(_ element: AXUIElement) -> CGPoint? {
        guard let frame = frame(element) else { return nil }
        return CGPoint(x: frame.midX, y: frame.midY)
    }

    private func frame(_ element: AXUIElement) -> CGRect? {
        guard let positionValue = attribute(element, kAXPositionAttribute as String),
              let sizeValue = attribute(element, kAXSizeAttribute as String),
              CFGetTypeID(positionValue) == AXValueGetTypeID(),
              CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return nil }
        var position = CGPoint.zero
        var size = CGSize.zero
        AXValueGetValue(positionValue as! AXValue, .cgPoint, &position)
        AXValueGetValue(sizeValue as! AXValue, .cgSize, &size)
        return CGRect(origin: position, size: size)
    }
}
