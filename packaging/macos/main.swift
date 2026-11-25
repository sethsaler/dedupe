import Cocoa
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, NSWindowDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var backendProcess: Process?
    private var backendOutputPipe: Pipe?
    private var backendErrorPipe: Pipe?
    private var serverURL: URL?
    private let nativeToken = UUID().uuidString
    private var pendingOutput = ""
    private var backendLog = ""
    private var isTerminating = false
    private var hasLoadedInterface = false

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
        guard let process = backendProcess, process.isRunning else { return .terminateNow }
        isTerminating = true

        DispatchQueue.global(qos: .userInitiated).async {
            if self.backendIsBusy() {
                DispatchQueue.main.async {
                    self.cancelBusyTermination(sender)
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
                if self.backendIsBusy() {
                    DispatchQueue.main.async {
                        self.cancelBusyTermination(sender)
                    }
                    return
                }
                process.terminate()
                Thread.sleep(forTimeInterval: 0.4)
            }
            DispatchQueue.main.async {
                sender.reply(toApplicationShouldTerminate: true)
            }
        }
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let process = backendProcess, process.isRunning {
            process.terminate()
        }
    }

    @objc private func reloadInterface() {
        if let serverURL, backendProcess?.isRunning == true {
            webView.load(URLRequest(url: serverURL))
        } else {
            serverURL = nil
            hasLoadedInterface = false
            showLoading()
            startBackend()
        }
    }

    @objc private func openInBrowser() {
        if let serverURL {
            NSWorkspace.shared.open(serverURL)
        }
    }

    private func startBackend() {
        if let process = backendProcess, process.isRunning {
            return
        }
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

        pendingOutput = ""
        backendLog = ""
        let process = Process()
        process.executableURL = executable
        // Keep the normal UI port as the first choice: its collision check
        // refuses a second Dedupe backend, and the stable loopback origin
        // preserves the review UI's saved settings across app launches.
        process.arguments = ["ui", "--port", "8765", "--no-browser"]

        var environment = ProcessInfo.processInfo.environment
        let existingPath = environment["PATH"] ?? "/usr/bin:/bin"
        environment["PATH"] = [
            "/opt/homebrew/bin",
            "/usr/local/bin",
            NSHomeDirectory() + "/.local/bin",
            existingPath,
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ].joined(separator: ":")
        environment.removeValue(forKey: "PYTHONHOME")
        environment.removeValue(forKey: "PYTHONPATH")
        environment.removeValue(forKey: "VIRTUAL_ENV")
        environment["DEDUPE_NATIVE_SHELL"] = "1"
        environment["DEDUPE_NATIVE_TOKEN"] = nativeToken
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
        } catch {
            backendProcess = nil
            showError(
                title: "Dedupe could not start",
                message: "macOS refused to launch the embedded backend.",
                detail: error.localizedDescription
            )
            return
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 15) { [weak self] in
            guard let self, self.serverURL == nil, !self.isTerminating else { return }
            if self.backendProcess?.isRunning == true {
                self.showError(
                    title: "Dedupe is taking too long to start",
                    message: "The embedded server did not report its local address within 15 seconds.",
                    detail: self.backendLog
                )
            }
        }
    }

    private func installOutputHandler(for pipe: Pipe, parseServerURL: Bool) {
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            DispatchQueue.main.async {
                self?.consumeBackendOutput(data, parseServerURL: parseServerURL)
            }
        }
    }

    private func consumeBackendOutput(_ data: Data, parseServerURL: Bool) {
        let text = String(decoding: data, as: UTF8.self)
        backendLog.append(text)
        if backendLog.count > 20_000 {
            backendLog = String(backendLog.suffix(16_000))
        }
        guard parseServerURL else { return }
        pendingOutput.append(text)
        while let newline = pendingOutput.firstIndex(of: "\n") {
            let line = String(pendingOutput[..<newline])
            pendingOutput = String(pendingOutput[pendingOutput.index(after: newline)...])
            consumeBackendLine(line)
        }
    }

    private func consumeBackendLine(_ line: String) {
        let prefix = "Dedupe UI: "
        guard serverURL == nil, line.hasPrefix(prefix) else { return }
        let value = line.dropFirst(prefix.count).trimmingCharacters(in: .whitespacesAndNewlines)
        guard let url = URL(string: value) else { return }
        serverURL = url
        hasLoadedInterface = true
        webView.load(URLRequest(url: url))
    }

    private func backendDidExit(_ process: Process) {
        guard !isTerminating else { return }
        let exitDetail = "The backend exited with status \(process.terminationStatus)."
        if serverURL == nil {
            showError(
                title: "Dedupe could not start",
                message: "The embedded server stopped before it was ready.",
                detail: [exitDetail, backendLog].joined(separator: "\n\n")
            )
        } else if hasLoadedInterface {
            serverURL = nil
            showError(
                title: "Dedupe stopped",
                message: "The embedded server exited unexpectedly.",
                detail: [exitDetail, backendLog].joined(separator: "\n\n")
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

    private func backendIsBusy() -> Bool {
        guard let serverURL else { return false }
        let endpoint = serverURL.appendingPathComponent("api/status")
        var request = URLRequest(url: endpoint)
        request.timeoutInterval = 0.5
        let semaphore = DispatchSemaphore(value: 0)
        var busy = false
        URLSession.shared.dataTask(with: request) { data, _, _ in
            if let data,
               let payload = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
                busy = (payload["scanning"] as? Bool == true) || (payload["acting"] as? Bool == true)
            }
            semaphore.signal()
        }.resume()
        _ = semaphore.wait(timeout: .now() + 0.7)
        return busy
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        guard backendIsBusy() else { return true }
        showBusyAlert()
        return false
    }

    private func cancelBusyTermination(_ sender: NSApplication) {
        isTerminating = false
        sender.reply(toApplicationShouldTerminate: false)
        // Let AppKit finish any vetoed close/termination pass before restoring
        // the reusable window and presenting modal UI.
        DispatchQueue.main.async {
            self.window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            self.showBusyAlert()
        }
    }

    private func showBusyAlert() {
        let alert = NSAlert()
        alert.messageText = "Dedupe is busy"
        alert.informativeText = "Wait for the current scan or file action to finish, then quit again."
        alert.alertStyle = .warning
        alert.addButton(withTitle: "OK")
        alert.runModal()
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
        fileMenu.addItem(item("Close Window", action: Selector("performClose:"), key: "w"))
        fileMenuItem.submenu = fileMenu

        let editMenuItem = NSMenuItem()
        mainMenu.addItem(editMenuItem)
        let editMenu = NSMenu(title: "Edit")
        editMenu.addItem(item("Undo", action: Selector("undo:"), key: "z"))
        editMenu.addItem(item("Redo", action: Selector("redo:"), key: "z", modifiers: [.command, .shift]))
        editMenu.addItem(.separator())
        editMenu.addItem(item("Cut", action: Selector("cut:"), key: "x"))
        editMenu.addItem(item("Copy", action: Selector("copy:"), key: "c"))
        editMenu.addItem(item("Paste", action: Selector("paste:"), key: "v"))
        editMenu.addItem(item("Select All", action: Selector("selectAll:"), key: "a"))
        editMenuItem.submenu = editMenu

        let viewMenuItem = NSMenuItem()
        mainMenu.addItem(viewMenuItem)
        let viewMenu = NSMenu(title: "View")
        viewMenu.addItem(item("Reload Dedupe", action: #selector(reloadInterface), key: "r", target: self))
        viewMenu.addItem(item("Open in Browser", action: #selector(openInBrowser), key: "o", target: self, modifiers: [.command, .shift]))
        viewMenu.addItem(.separator())
        viewMenu.addItem(item("Enter Full Screen", action: Selector("toggleFullScreen:"), key: "f", modifiers: [.command, .control]))
        viewMenuItem.submenu = viewMenu

        let windowMenuItem = NSMenuItem()
        mainMenu.addItem(windowMenuItem)
        let windowMenu = NSMenu(title: "Window")
        windowMenu.addItem(item("Minimize", action: Selector("performMiniaturize:"), key: "m"))
        windowMenu.addItem(item("Zoom", action: Selector("performZoom:"), key: ""))
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
