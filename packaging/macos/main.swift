import Cocoa
import Darwin
import WebKit

private final class ScriptMessageProxy: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?

    init(target: WKScriptMessageHandler) {
        self.target = target
    }

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate, WKScriptMessageHandler {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backendProcess: Process?
    private var backendOutputPipe: Pipe?
    private var backendErrorPipe: Pipe?
    private var serverURL: URL?
    private let nativeToken = UUID().uuidString
    private var isTerminating = false
    private var hasLoadedInterface = false
    private var isBackendBusy = false
    private var hasLiveBusyState = false
    private var allowConfirmedWindowClose = false
    private var isCheckingWindowClose = false
    private var isPickerOpen = false
    private var serverActivity: NSObjectProtocol?
    private var busyActivity: NSObjectProtocol?

    private let logQueue = DispatchQueue(label: "com.sethsaler.dedupe.backend-log", qos: .utility)
    private var backendLogBytes = Data()
    private var pendingStdoutBuffer = Data()
    private var serverURLReported = false
    private let maxLogBytes = 20_000
    private let trimmedLogBytes = 16_000

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenus()
        NSApp.setActivationPolicy(.regular)

        let frame = NSRect(x: 0, y: 0, width: 1280, height: 860)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Dedupe"
        window.minSize = NSSize(width: 900, height: 620)
        window.isReleasedWhenClosed = false
        window.delegate = self
        window.setFrameAutosaveName("DedupeMainWindow")
        window.center()

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        let proxy = ScriptMessageProxy(target: self)
        configuration.userContentController.add(proxy, name: "busyState")
        configuration.userContentController.add(proxy, name: "pickPaths")
        webView = WKWebView(frame: window.contentLayoutRect, configuration: configuration)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView

        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        showLoading()
        startBackend()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard !isTerminating else { return .terminateLater }
        guard let process = backendProcess, process.isRunning else {
            endAllActivities()
            return .terminateNow
        }
        isTerminating = true
        let cachedBusy = isBackendBusy

        DispatchQueue.global(qos: .userInitiated).async {
            let status = self.queryBackendBusy(cachedBusy: cachedBusy)
            if status.busy {
                DispatchQueue.main.async {
                    self.cancelBusyTermination(sender, unresponsive: status.unresponsive)
                }
                return
            }
            self.requestBackendShutdown()
            // The endpoint deliberately waits through the browser shutdown grace
            // period. Give that clean path a moment, then stop the embedded server.
            let deadline = Date().addingTimeInterval(2.2)
            while process.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if process.isRunning {
                // A scan or file action can have started after the shutdown POST.
                // Never kill it halfway through a batch of file mutations.
                let recheck = self.queryBackendBusy(cachedBusy: self.currentBusyFlag())
                if recheck.busy {
                    DispatchQueue.main.async {
                        self.cancelBusyTermination(sender, unresponsive: recheck.unresponsive)
                    }
                    return
                }
                self.terminateBackendTree(process, graceSeconds: 0.4)
            }
            DispatchQueue.main.async {
                self.endAllActivities()
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = backendProcess, process.isRunning {
            terminateBackendTree(process, graceSeconds: 0.2)
        }
        cleanupBackendPipes()
        endAllActivities()
    }

    @objc private func reloadInterface() {
        if let serverURL, backendProcess?.isRunning == true {
            webView.load(URLRequest(url: serverURL))
        } else {
            serverURL = nil
            hasLoadedInterface = false
            updateBusyState(false, live: false)
            showLoading()
            startBackend()
        }
    }

    @objc private func openInBrowser() {
        if let serverURL {
            NSWorkspace.shared.open(serverURL)
        }
    }

    // MARK: - Power / App Nap management

    private func beginServerActivity() {
        guard serverActivity == nil else { return }
        serverActivity = ProcessInfo.processInfo.beginActivity(
            options: [.userInitiatedAllowingIdleSystemSleep],
            reason: "Dedupe local server active"
        )
    }

    private func endServerActivity() {
        if let token = serverActivity {
            ProcessInfo.processInfo.endActivity(token)
            serverActivity = nil
        }
    }

    private func updateBusyState(_ busy: Bool, live: Bool) {
        isBackendBusy = busy
        if live {
            hasLiveBusyState = true
        }
        if busy {
            if busyActivity == nil {
                busyActivity = ProcessInfo.processInfo.beginActivity(
                    options: [.userInitiated, .idleSystemSleepDisabled],
                    reason: "Dedupe media scan or file action in progress"
                )
            }
        } else if let token = busyActivity {
            ProcessInfo.processInfo.endActivity(token)
            busyActivity = nil
        }
    }

    private func endAllActivities() {
        updateBusyState(false, live: false)
        endServerActivity()
    }

    private func currentBusyFlag() -> Bool {
        if Thread.isMainThread {
            return isBackendBusy
        }
        return DispatchQueue.main.sync { self.isBackendBusy }
    }

    // MARK: - WKScriptMessageHandler

    func userContentController(
        _ userContentController: WKUserContentController,
        didReceive message: WKScriptMessage
    ) {
        switch message.name {
        case "busyState":
            if let body = message.body as? [String: Any], let busy = body["busy"] as? Bool {
                updateBusyState(busy, live: true)
            }
        case "pickPaths":
            handlePickPathsMessage(message.body)
        default:
            break
        }
    }

    private func handlePickPathsMessage(_ rawBody: Any) {
        guard let body = rawBody as? [String: Any],
              let requestId = body["id"] as? String else {
            return
        }
        let kind = (body["kind"] as? String ?? "folder").lowercased()
        if isPickerOpen {
            resolvePickPaths(
                requestId: requestId,
                payload: [
                    "cancelled": true,
                    "path": NSNull(),
                    "paths": [],
                    "busy": true,
                ]
            )
            return
        }

        isPickerOpen = true
        let panel = NSOpenPanel()
        panel.allowsMultipleSelection = true
        panel.canCreateDirectories = false
        panel.resolvesAliases = true
        panel.prompt = "Choose"
        if kind == "files" {
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.message = "Choose media files to scan"
        } else {
            panel.canChooseFiles = false
            panel.canChooseDirectories = true
            panel.message = "Choose folders to scan"
        }

        panel.beginSheetModal(for: window) { [weak self] response in
            guard let self else { return }
            self.isPickerOpen = false
            guard response == .OK else {
                self.resolvePickPaths(
                    requestId: requestId,
                    payload: [
                        "cancelled": true,
                        "path": NSNull(),
                        "paths": [],
                    ]
                )
                return
            }
            let paths = panel.urls.map { $0.standardizedFileURL.path }
            if paths.isEmpty {
                self.resolvePickPaths(
                    requestId: requestId,
                    payload: [
                        "cancelled": true,
                        "path": NSNull(),
                        "paths": [],
                    ]
                )
            } else {
                self.resolvePickPaths(
                    requestId: requestId,
                    payload: [
                        "cancelled": false,
                        "path": paths[0],
                        "paths": paths,
                    ]
                )
            }
        }
    }

    private func resolvePickPaths(requestId: String, payload: [String: Any]) {
        guard let idData = try? JSONSerialization.data(withJSONObject: [requestId]),
              let idArrayStr = String(data: idData, encoding: .utf8),
              let payloadData = try? JSONSerialization.data(withJSONObject: payload),
              let payloadStr = String(data: payloadData, encoding: .utf8) else {
            return
        }
        let script = "window.__dedupeResolvePick && window.__dedupeResolvePick(\(idArrayStr)[0], \(payloadStr));"
        webView.evaluateJavaScript(script, completionHandler: nil)
    }

    // MARK: - Backend process lifecycle

    private func finderSafePath(existing: String?) -> String {
        let candidates = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/opt/local/bin",
            NSHomeDirectory() + "/.local/bin",
            "/run/current-system/sw/bin",
            NSHomeDirectory() + "/.nix-profile/bin",
            existing ?? "",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        var seen = Set<String>()
        var parts: [String] = []
        for entry in candidates {
            for part in entry.split(separator: ":").map(String.init) where !part.isEmpty {
                if seen.insert(part).inserted {
                    parts.append(part)
                }
            }
        }
        return parts.joined(separator: ":")
    }

    private func startBackend() {
        if let process = backendProcess, process.isRunning {
            return
        }
        cleanupBackendPipes()
        guard let resourceURL = Bundle.main.resourceURL else {
            showError(title: "Dedupe could not start", message: "The application resources are unavailable.")
            return
        }
        let executable = resourceURL
            .appendingPathComponent("backend", isDirectory: true)
            .appendingPathComponent("dedupe-server", isDirectory: false)
        guard FileManager.default.isExecutableFile(atPath: executable.path) else {
            showError(
                title: "Dedupe is incomplete",
                message: "The embedded backend is missing. Rebuild or reinstall the app."
            )
            return
        }

        logQueue.sync {
            self.backendLogBytes.removeAll(keepingCapacity: false)
            self.pendingStdoutBuffer.removeAll(keepingCapacity: false)
            self.serverURLReported = false
        }

        let process = Process()
        process.executableURL = executable
        // Keep the normal UI port as the first choice: its collision check
        // refuses a second Dedupe backend, and the stable loopback origin
        // preserves the review UI's saved settings across app launches.
        process.arguments = ["ui", "--port", "8765", "--no-browser"]

        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = finderSafePath(existing: environment["PATH"])
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "VIRTUAL_ENV")
        environment["DEDUPE_NATIVE_SHELL"] = "1"
        environment["DEDUPE_NATIVE_TOKEN"] = nativeToken
        environment["DEDUPE_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        environment["PYTHONUNBUFFERED"] = "1"
        process.environment = environment

        let outputPipe = Pipe()
        let errorPipe = Pipe()
        process.standardOutput = outputPipe
        process.standardError = errorPipe
        backendOutputPipe = outputPipe
        backendErrorPipe = errorPipe
        installOutputHandler(for: outputPipe, parseServerURL: true)
        installOutputHandler(for: errorPipe, parseServerURL: false)

        process.terminationHandler = { [weak self] finishedProcess in
            DispatchQueue.main.async {
                self?.backendDidExit(finishedProcess)
            }
        }

        do {
            backendProcess = process
            try process.run()
            beginServerActivity()
        } catch {
            backendProcess = nil
            cleanupBackendPipes()
            showError(
                title: "Dedupe could not start",
                message: "macOS refused to launch the embedded backend.",
                detail: error.localizedDescription
            )
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 30) { [weak self] in
            guard let self, self.serverURL == nil, !self.isTerminating else { return }
            guard self.backendProcess === process, process.isRunning else { return }
            self.terminateBackendTree(process, graceSeconds: 0.3)
            self.showError(
                title: "Dedupe is taking too long to start",
                message: "The embedded server did not report its local address within 30 seconds.",
                detail: self.currentBackendLog()
            )
        }
    }

    private func cleanupBackendPipes() {
        for pipe in [backendOutputPipe, backendErrorPipe] {
            guard let pipe else { continue }
            pipe.fileHandleForReading.readabilityHandler = nil
            try? pipe.fileHandleForReading.close()
        }
        backendOutputPipe = nil
        backendErrorPipe = nil
    }

    private func installOutputHandler(for pipe: Pipe, parseServerURL: Bool) {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty {
                // Immediately detach on EOF to avoid GCD's 100% CPU busy-loop.
                handle.readabilityHandler = nil
                try? handle.close()
                return
            }
            guard let self else {
                handle.readabilityHandler = nil
                return
            }
            self.logQueue.async {
                self.consumeBackendOutputOnLogQueue(data, parseServerURL: parseServerURL)
            }
        }
    }

    private func consumeBackendOutputOnLogQueue(_ data: Data, parseServerURL: Bool) {
        backendLogBytes.append(data)
        if backendLogBytes.count > maxLogBytes {
            backendLogBytes = Data(backendLogBytes.suffix(trimmedLogBytes))
        }
        guard parseServerURL, !serverURLReported else { return }
        pendingStdoutBuffer.append(data)
        let newline = UInt8(ascii: "\n")
        while let idx = pendingStdoutBuffer.firstIndex(of: newline) {
            let lineData = pendingStdoutBuffer[..<idx]
            pendingStdoutBuffer.removeSubrange(...idx)
            let line = String(decoding: lineData, as: UTF8.self)
            let prefix = "Dedupe UI: "
            if line.hasPrefix(prefix) {
                let value = line.dropFirst(prefix.count).trimmingCharacters(in: .whitespacesAndNewlines)
                if let url = URL(string: value) {
                    serverURLReported = true
                    pendingStdoutBuffer.removeAll(keepingCapacity: false)
                    DispatchQueue.main.async { [weak self] in
                        self?.didDiscoverServerURL(url)
                    }
                    return
                }
            }
        }
        if pendingStdoutBuffer.count > maxLogBytes {
            pendingStdoutBuffer = Data(pendingStdoutBuffer.suffix(trimmedLogBytes))
        }
    }

    private func currentBackendLog() -> String {
        logQueue.sync {
            String(decoding: backendLogBytes, as: UTF8.self)
        }
    }

    private func didDiscoverServerURL(_ url: URL) {
        guard serverURL == nil else { return }
        serverURL = url
        hasLoadedInterface = true
        webView.load(URLRequest(url: url))
    }

    private func terminateBackendTree(_ process: Process, graceSeconds: TimeInterval) {
        guard process.isRunning else { return }
        let pid = process.processIdentifier
        if pid > 0 {
            // Backend calls os.setpgrp(), so signaling -pid reaches child
            // ffmpeg / ProcessPoolExecutor / helper processes as well.
            if kill(-pid, SIGTERM) != 0 {
                process.terminate()
            }
        } else {
            process.terminate()
        }
        let deadline = Date().addingTimeInterval(graceSeconds)
        while process.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
        if process.isRunning && pid > 0 {
            if kill(-pid, SIGKILL) != 0 {
                kill(pid, SIGKILL)
            }
        }
    }

    private func backendDidExit(_ process: Process) {
        cleanupBackendPipes()
        endAllActivities()
        guard !isTerminating else { return }
        let exitDetail = "The backend exited with status \(process.terminationStatus)."
        let logText = currentBackendLog()
        if serverURL == nil {
            showError(
                title: "Dedupe could not start",
                message: "The embedded server stopped before it was ready.",
                detail: [exitDetail, logText].joined(separator: "\n\n")
            )
        } else if hasLoadedInterface {
            serverURL = nil
            showError(
                title: "Dedupe stopped",
                message: "The embedded server exited unexpectedly.",
                detail: [exitDetail, logText].joined(separator: "\n\n")
            )
        }
    }

    private func requestBackendShutdown() {
        guard let serverURL else { return }
        let endpoint = serverURL.appendingPathComponent("api/shutdown")
        var request = URLRequest(url: endpoint)
        request.httpMethod = "POST"
        request.timeoutInterval = 0.6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(nativeToken, forHTTPHeaderField: "X-Dedupe-Native-Token")
        request.httpBody = Data("{}".utf8)
        let semaphore = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { _, _, _ in
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 0.8)
    }

    private func queryBackendBusy(cachedBusy: Bool) -> (busy: Bool, unresponsive: Bool) {
        if cachedBusy {
            return (true, false)
        }
        guard let serverURL, backendProcess?.isRunning == true else {
            return (false, false)
        }
        let endpoint = serverURL.appendingPathComponent("api/status")
        var request = URLRequest(url: endpoint)
        request.timeoutInterval = 0.8
        let semaphore = DispatchSemaphore(value: 0)
        var gotPayload = false
        var busy = false
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data,
               let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                gotPayload = true
                busy = (payload["scanning"] as? Bool == true) || (payload["acting"] as? Bool == true)
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 1.0)
        if !gotPayload {
            // Fail closed while the backend is still running so an overloaded
            // scan/action is never SIGTERMed on a status timeout.
            let stillRunning = backendProcess?.isRunning == true
            return (stillRunning, stillRunning)
        }
        return (busy, false)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        if allowConfirmedWindowClose {
            allowConfirmedWindowClose = false
            return true
        }
        if isBackendBusy {
            showBusyAlert(unresponsive: false, onForceQuit: nil)
            return false
        }
        if hasLiveBusyState || backendProcess?.isRunning != true {
            return true
        }
        // Fallback before JS has reported live state: check off the main thread.
        guard !isCheckingWindowClose else { return false }
        isCheckingWindowClose = true
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let status = self.queryBackendBusy(cachedBusy: false)
            DispatchQueue.main.async {
                self.isCheckingWindowClose = false
                if status.busy {
                    self.showBusyAlert(unresponsive: status.unresponsive) {
                        self.allowConfirmedWindowClose = true
                        self.window.close()
                    }
                } else {
                    self.allowConfirmedWindowClose = true
                    self.window.close()
                }
            }
        }
        return false
    }

    private func cancelBusyTermination(_ sender: NSApplication, unresponsive: Bool) {
        isTerminating = false
        sender.reply(toApplicationShouldTerminate: false)
        // Let AppKit finish any vetoed close/termination pass before restoring
        // the reusable window and presenting modal UI.
        DispatchQueue.main.async {
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            self.showBusyAlert(unresponsive: unresponsive) {
                self.isTerminating = true
                if let process = self.backendProcess, process.isRunning {
                    self.terminateBackendTree(process, graceSeconds: 0.3)
                }
                self.endAllActivities()
                NSApp.terminate(nil)
            }
        }
    }

    private func showBusyAlert(unresponsive: Bool, onForceQuit: (() -> Void)?) {
        let alert = NSAlert()
        alert.alertStyle = .warning
        if unresponsive {
            alert.messageText = "Dedupe is busy or not responding"
            alert.informativeText = "The local media server did not respond in time. Wait for active work to finish, or force quit now."
            alert.addButton(withTitle: "Wait")
            if onForceQuit != nil {
                alert.addButton(withTitle: "Force Quit")
            }
        } else {
            alert.messageText = "Dedupe is busy"
            alert.informativeText = "Wait for the current scan or file action to finish, then quit again."
            alert.addButton(withTitle: "OK")
        }
        let response = alert.runModal()
        if unresponsive && response == .alertSecondButtonReturn {
            onForceQuit?()
        }
    }

    private func showLoading() {
        webView.loadHTMLString(
            """
            <!doctype html>
            <html><head><meta charset="utf-8"><title>Dedupe</title><style>
            body{margin:0;height:100vh;display:grid;place-items:center;background:#f5f5f7;color:#1d1d1f;font:15px -apple-system,BlinkMacSystemFont,sans-serif}
            main{text-align:center}.mark{font-size:56px;line-height:1;margin-bottom:18px}h1{font-size:28px;margin:0 0 8px}p{color:#6e6e73;margin:0}
            .spinner{width:28px;height:28px;border:3px solid #d2d2d7;border-top-color:#007aff;border-radius:50%;margin:28px auto 0;animation:spin 0.9s linear infinite}
            @keyframes spin{to{transform:rotate(360deg)}}
            @media (prefers-color-scheme: dark){body{background:#1c1c1e;color:#f5f5f7}p{color:#98989d}}
            </style></head><body><main><div class="mark">▦</div><h1>Dedupe</h1><p>Starting the media server…</p><div class="spinner"></div></main></body></html>
            """,
            baseURL: nil
        )
    }

    private func showError(title: String, message: String, detail: String? = nil) {
        let detailHTML: String
        if let detail, !detail.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            detailHTML = "<details open><summary>Details</summary><pre>\(escapeHTML(detail))</pre></details>"
        } else {
            detailHTML = ""
        }
        webView.loadHTMLString(
            """
            <!doctype html>
            <html><head><meta charset="utf-8"><title>\(escapeHTML(title))</title><style>
            body{margin:0;min-height:100vh;display:grid;place-items:center;background:#f5f5f7;color:#1d1d1f;font:15px -apple-system,BlinkMacSystemFont,sans-serif}
            main{max-width:720px;padding:40px}h1{font-size:28px;margin:0 0 12px}p{line-height:1.5;color:#48484a}pre{white-space:pre-wrap;word-break:break-word;background:rgba(0,0,0,0.06);padding:12px;border-radius:8px;max-height:320px;overflow:auto;font:12px ui-monospace,Menlo,monospace}
            @media (prefers-color-scheme: dark){body{background:#1c1c1e;color:#f5f5f7}p{color:#c7c7cc}pre{background:rgba(255,255,255,0.08)}}
            </style></head><body><main><h1>\(escapeHTML(title))</h1><p>\(escapeHTML(message))</p>\(detailHTML)</main></body></html>
            """,
            baseURL: nil
        )
    }

    private func escapeHTML(_ value: String) -> String {
        value
            .replacingOccurrences(of: "&", with: "&amp;")
            .replacingOccurrences(of: "<", with: "&lt;")
            .replacingOccurrences(of: ">", with: "&gt;")
            .replacingOccurrences(of: "\"", with: "&quot;")
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        guard let url = navigationAction.request.url,
              let scheme = url.scheme,
              ["http", "https"].contains(scheme),
              let host = url.host?.lowercased(),
              host != "127.0.0.1",
              host != "localhost",
              host != "::1"
        else {
            decisionHandler(.allow)
            return
        }
        NSWorkspace.shared.open(url)
        decisionHandler(.cancel)
    }

    private func installMenus() {
        func item(
            _ title: String,
            action: Selector?,
            key: String,
            target: AnyObject? = nil,
            modifiers: NSEvent.ModifierFlags = .command
        ) -> NSMenuItem {
            let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: key)
            menuItem.keyEquivalentModifierMask = modifiers
            menuItem.target = target
            return menuItem
        }

        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        mainMenu.addItem(appMenuItem)
        let appMenu = NSMenu(title: "Dedupe")
        appMenu.addItem(item("About Dedupe", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), key: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Hide Dedupe", action: #selector(NSApplication.hide(_:)), key: "h"))
        appMenu.addItem(item("Hide Others", action: #selector(NSApplication.hideOtherApplications(_:)), key: "h", modifiers: [.command, .option]))
        appMenu.addItem(item("Show All", action: #selector(NSApplication.unhideAllApplications(_:)), key: ""))
        appMenu.addItem(.separator())
        appMenu.addItem(item("Quit Dedupe", action: #selector(NSApplication.terminate(_:)), key: "q"))
        appMenuItem.submenu = appMenu

        let fileMenuItem = NSMenuItem()
        mainMenu.addItem(fileMenuItem)
        let fileMenu = NSMenu(title: "File")
        fileMenu.addItem(item("Close Window", action: #selector(NSWindow.performClose(_:)), key: "w"))
        fileMenuItem.submenu = fileMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(item("Undo", action: Selector(("undo:")), key: "z"))
        editMenu.addItem(item("Redo", action: Selector(("redo:")), key: "z", modifiers: [.command, .shift]))
        editMenu.addItem(.separator())
        editMenu.addItem(item("Cut", action: #selector(NSText.cut(_:)), key: "x"))
        editMenu.addItem(item("Copy", action: #selector(NSText.copy(_:)), key: "c"))
        editMenu.addItem(item("Paste", action: #selector(NSText.paste(_:)), key: "v"))
        editMenu.addItem(item("Select All", action: #selector(NSText.selectAll(_:)), key: "a"))
        editMenuItem.submenu = editMenu

        let viewMenuItem = NSMenuItem()
        mainMenu.addItem(viewMenuItem)
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(item("Reload Dedupe", action: #selector(reloadInterface), key: "r", target: self))
        viewMenu.addItem(item("Open in Browser", action: #selector(openInBrowser), key: "o", target: self, modifiers: [.command, .shift]))
        viewMenu.addItem(.separator())
        viewMenu.addItem(item("Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), key: "f", modifiers: [.command, .control]))
        viewMenuItem.submenu = viewMenu

        let windowMenuItem = NSMenuItem()
        mainMenu.addItem(windowMenuItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(item("Minimize", action: #selector(NSWindow.performMiniaturize(_:)), key: "m"))
        windowMenu.addItem(item("Zoom", action: #selector(NSWindow.performZoom(_:)), key: ""))
        windowMenuItem.submenu = windowMenu

        NSApp.mainMenu = mainMenu
        NSApp.windowsMenu = windowMenu
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
