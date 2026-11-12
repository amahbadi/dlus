// SPDX-License-Identifier: MIT
// Copyright (c) 2026 Ali Akbar Mahbadi and dlus contributors
// dlus.app - native window for dlus. The work (downloads, TV, sharing) runs in a background
// service (launchd agent com.dlus, installed by setup.sh); this app shows its UI in a WebKit view.
import AppKit
import UserNotifications
import WebKit

// `dlus --notify <title> <message>`: the background service shows its notifications through the app, so macOS
// shows the dlus icon (and a click opens dlus). No window, no Dock icon; exits when the notification is handed over.
if CommandLine.arguments.count >= 4 && CommandLine.arguments[1] == "--notify" {
    let center = UNUserNotificationCenter.current()
    let done = DispatchSemaphore(value: 0)
    center.requestAuthorization(options: [.alert, .sound]) { granted, _ in
        guard granted else {
            done.signal()
            return
        }
        let content = UNMutableNotificationContent()
        content.title = CommandLine.arguments[2]
        content.body = CommandLine.arguments[3]
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        center.add(request) { _ in done.signal() }
    }
    _ = done.wait(timeout: .now() + 120)  // the very first time macOS asks the user to allow notifications
    exit(0)
}

let uiURL = URL(string: "http://127.0.0.1:8642/")!
let keyFile = (NSHomeDirectory() as NSString).appendingPathComponent(".dlus/ui-key")

// the UI key (~/.dlus/ui-key, readable only by this user): without it the dlus page refuses to work
func uiKey() -> String { (try? String(contentsOfFile: keyFile, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines) ?? "" }
func keyed(_ u: URL) -> URL {
    var c = URLComponents(url: u, resolvingAgainstBaseURL: false)!
    c.queryItems = (c.queryItems ?? []).filter { $0.name != "k" } + [URLQueryItem(name: "k", value: uiKey())]
    return c.url!
}
let healthURL = URL(string: "http://127.0.0.1:8642/api/health")!

func isLocal(_ url: URL?) -> Bool {
    guard let h = url?.host else { return false }
    return h == "127.0.0.1" || h == "localhost"
}

func isPrivateLAN(_ host: String) -> Bool {
    let p = host.split(separator: ".").compactMap { Int($0) }
    guard p.count == 4 else { return false }
    return p[0] == 10 || (p[0] == 172 && (16...31).contains(p[1])) || (p[0] == 192 && p[1] == 168)
}

final class AppDelegate: NSObject, NSApplicationDelegate, WKNavigationDelegate, WKUIDelegate {
    var window: NSWindow!
    var web: WKWebView!
    var status: NSTextField!
    var spinner: NSProgressIndicator!
    var cover: NSView!
    var retry: NSButton!
    var badgeTimer: Timer?

    func applicationDidFinishLaunching(_ note: Notification) {
        buildMenu()
        let cfg = WKWebViewConfiguration()
        cfg.mediaTypesRequiringUserActionForPlayback = []
        cfg.allowsAirPlayForMediaPlayback = true
        if #available(macOS 12.3, *) { cfg.preferences.isElementFullscreenEnabled = true }
        cfg.preferences.javaScriptCanOpenWindowsAutomatically = true
        web = WKWebView(frame: .zero, configuration: cfg)
        web.navigationDelegate = self
        web.uiDelegate = self
        web.allowsMagnification = true
        web.allowsBackForwardNavigationGestures = true
        web.setValue(false, forKey: "drawsBackground")  // no white flash while the page loads
        web.customUserAgent = nil

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1320, height: 860),
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered, defer: false)
        window.title = "dlus"
        window.titlebarAppearsTransparent = true
        window.backgroundColor = NSColor(red: 0.04, green: 0.04, blue: 0.05, alpha: 1)
        window.minSize = NSSize(width: 900, height: 600)
        window.center()
        window.setFrameAutosaveName("dlus-main")
        window.collectionBehavior = [.fullScreenPrimary]
        window.contentView = web
        addCover()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        startService()
    }

    // ---------------------------------------------------------------- start-up: service + wait for it
    func addCover() {
        cover = NSView()
        cover.wantsLayer = true
        cover.layer?.backgroundColor = window.backgroundColor.cgColor
        cover.translatesAutoresizingMaskIntoConstraints = false
        let logo = NSImageView(image: NSApp.applicationIconImage)
        logo.translatesAutoresizingMaskIntoConstraints = false
        spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.startAnimation(nil)
        status = NSTextField(labelWithString: "Starting dlus...")
        status.textColor = NSColor(white: 0.75, alpha: 1)
        status.alignment = .center
        status.translatesAutoresizingMaskIntoConstraints = false
        status.maximumNumberOfLines = 4
        status.preferredMaxLayoutWidth = 460
        retry = NSButton(title: "Try again", target: self, action: #selector(retryStart))
        retry.translatesAutoresizingMaskIntoConstraints = false
        retry.isHidden = true
        for v in [logo, spinner, status, retry] as [NSView] { cover.addSubview(v) }
        web.addSubview(cover)
        NSLayoutConstraint.activate([
            cover.leadingAnchor.constraint(equalTo: web.leadingAnchor), cover.trailingAnchor.constraint(equalTo: web.trailingAnchor),
            cover.topAnchor.constraint(equalTo: web.topAnchor), cover.bottomAnchor.constraint(equalTo: web.bottomAnchor),
            logo.centerXAnchor.constraint(equalTo: cover.centerXAnchor), logo.centerYAnchor.constraint(equalTo: cover.centerYAnchor, constant: -50),
            logo.widthAnchor.constraint(equalToConstant: 96), logo.heightAnchor.constraint(equalToConstant: 96),
            spinner.centerXAnchor.constraint(equalTo: cover.centerXAnchor), spinner.topAnchor.constraint(equalTo: logo.bottomAnchor, constant: 18),
            status.centerXAnchor.constraint(equalTo: cover.centerXAnchor), status.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 10),
            status.widthAnchor.constraint(lessThanOrEqualToConstant: 460),
            retry.centerXAnchor.constraint(equalTo: cover.centerXAnchor), retry.topAnchor.constraint(equalTo: status.bottomAnchor, constant: 14),
        ])
    }

    func startService() {
        guard let setup = Bundle.main.url(forResource: "setup", withExtension: "sh") else { return fail("setup.sh is missing from the app") }
        DispatchQueue.global().async {
            let p = Process()
            p.executableURL = URL(fileURLWithPath: "/bin/bash")
            p.arguments = [setup.path]
            let pipe = Pipe()
            p.standardError = pipe
            do { try p.run() } catch {
                DispatchQueue.main.async { self.fail("Could not start dlus: \(error.localizedDescription)") }
                return
            }
            p.waitUntilExit()
            let err = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            if p.terminationStatus != 0 {
                DispatchQueue.main.async { self.fail(err.isEmpty ? "dlus could not start its background service." : err) }
                return
            }
            self.waitForService(tries: 60)
        }
    }

    func waitForService(tries: Int) {
        var req = URLRequest(url: healthURL)
        req.timeoutInterval = 1
        URLSession.shared.dataTask(with: req) { _, resp, _ in
            DispatchQueue.main.async {
                if (resp as? HTTPURLResponse)?.statusCode == 200 {
                    if self.wantsBrowser() { return }
                    self.status.stringValue = "Opening..."
                    self.web.load(URLRequest(url: keyed(uiURL)))
                    self.startBadge()
                } else if tries > 0 {
                    if tries == 50 { self.status.stringValue = "Starting the background service..." }
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.waitForService(tries: tries - 1) }
                } else {
                    self.fail("dlus did not start. Details are in ~/.dlus/launchd.log")
                }
            }
        }.resume()
    }

    // Settings -> "Open dlus in: my web browser" (hold Option while opening dlus to get the window anyway)
    func wantsBrowser() -> Bool {
        if NSEvent.modifierFlags.contains(.option) { return false }
        var req = URLRequest(url: URL(string: "http://127.0.0.1:8642/api/state")!)
        req.setValue("dlus_k=" + uiKey(), forHTTPHeaderField: "Cookie")
        req.timeoutInterval = 3
        let sem = DispatchSemaphore(value: 0)
        var browser = false
        URLSession.shared.dataTask(with: req) { data, _, _ in
            if let data = data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                let st = j["settings"] as? [String: Any], (st["ui_open"] as? String) == "browser"
            {
                browser = true
            }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + 3)
        if browser {
            NSWorkspace.shared.open(keyed(uiURL))
            NSApp.terminate(nil)
        }
        return browser
    }

    func js(_ code: String) {
        window.makeKeyAndOrderFront(nil)
        web.evaluateJavaScript(code, completionHandler: nil)
    }
    @objc func newDownload() { js("openAdd()") }
    @objc func openSettings() { js("openSet()") }
    @objc func findInDlus() { js("document.getElementById('q').focus()") }
    @objc func goView(_ sender: NSMenuItem) { js("setView('\(sender.representedObject as? String ?? "all")')") }

    func fail(_ msg: String) {
        cover.isHidden = false
        spinner.isHidden = true
        status.stringValue = msg.trimmingCharacters(in: .whitespacesAndNewlines)
        retry.isHidden = false
    }

    @objc func retryStart() {
        retry.isHidden = true
        spinner.isHidden = false
        status.stringValue = "Starting dlus..."
        startService()
    }

    // ---------------------------------------------------------------- Dock badge: running downloads
    func startBadge() {
        badgeTimer?.invalidate()
        badgeTimer = Timer.scheduledTimer(withTimeInterval: 3, repeats: true) { _ in
            var req = URLRequest(url: URL(string: "http://127.0.0.1:8642/api/state")!)
            req.setValue("dlus_k=" + uiKey(), forHTTPHeaderField: "Cookie")
            URLSession.shared.dataTask(with: req) { data, _, _ in
                guard let data = data, let j = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                    let items = j["items"] as? [[String: Any]]
                else { return }
                let n = items.filter { ($0["status"] as? String) == "downloading" }.count
                DispatchQueue.main.async { NSApp.dockTile.badgeLabel = n > 0 ? String(n) : nil }
            }.resume()
        }
    }

    // ---------------------------------------------------------------- web view behaviour
    func webView(_ w: WKWebView, didFinish nav: WKNavigation!) {
        if isLocal(w.url) {
            NSAnimationContext.runAnimationGroup(
                { c in
                    c.duration = 0.25
                    self.cover.animator().alphaValue = 0
                },
                completionHandler: {
                    self.cover.isHidden = true
                    self.cover.alphaValue = 1
                })
        }
    }

    func webView(_ w: WKWebView, didFailProvisionalNavigation nav: WKNavigation!, withError error: Error) {
        if isLocal(w.url) || w.url == nil { fail("dlus is not answering. It may be restarting - try again in a moment.") }
    }

    // links to the internet open in the normal browser; dlus pages stay in the window
    func webView(_ w: WKWebView, decidePolicyFor action: WKNavigationAction, decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, ["http", "https"].contains(u.scheme ?? ""), !isLocal(u), action.targetFrame?.isMainFrame ?? true {
            NSWorkspace.shared.open(u)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    // files the window cannot show (subtitles, playlists, downloads) go to the browser / default app
    func webView(_ w: WKWebView, decidePolicyFor resp: WKNavigationResponse, decisionHandler: @escaping (WKNavigationResponsePolicy) -> Void) {
        if !resp.canShowMIMEType, let u = resp.response.url {
            NSWorkspace.shared.open(u)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    // target=_blank / window.open
    var players: [NSWindow] = []

    func webView(_ w: WKWebView, createWebViewWith cfg: WKWebViewConfiguration, for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView?
    {
        guard let u = action.request.url else { return nil }
        if !isLocal(u) {
            NSWorkspace.shared.open(u)
            return nil
        }
        if u.port != 8644 {
            w.load(action.request)
            return nil
        }
        // the film plays in its own window: closing it (red button / Cmd-W / Esc in the player) returns to dlus
        let pv = WKWebView(frame: .zero, configuration: cfg)
        pv.navigationDelegate = self
        pv.uiDelegate = self
        pv.setValue(false, forKey: "drawsBackground")
        let pw = NSWindow(
            contentRect: window.frame, styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        pw.title = "dlus player"
        pw.titlebarAppearsTransparent = true
        pw.titleVisibility = .hidden
        pw.backgroundColor = .black
        pw.isReleasedWhenClosed = false
        pw.collectionBehavior = [.fullScreenPrimary]
        pw.contentView = pv
        pw.setFrame(window.frame, display: true)
        pw.makeKeyAndOrderFront(nil)
        players.append(pw)
        NotificationCenter.default.addObserver(forName: NSWindow.willCloseNotification, object: pw, queue: .main) { [weak self] _ in
            pv.loadHTMLString("", baseURL: nil)  // stop the film when its window closes
            self?.players.removeAll { $0 === pw }
            self?.window.makeKeyAndOrderFront(nil)
        }
        return pv
    }

    // window.close() from the player page closes its window
    func webViewDidClose(_ w: WKWebView) { w.window?.performClose(nil) }

    // the TV's app icons come from its own self-signed HTTPS server on the home network
    func webView(
        _ w: WKWebView, didReceive challenge: URLAuthenticationChallenge,
        completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void
    ) {
        // only the TV's own service port on the home network - never internet sites
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
            challenge.protectionSpace.port == 3001, isPrivateLAN(challenge.protectionSpace.host), let t = challenge.protectionSpace.serverTrust
        {
            return completionHandler(.useCredential, URLCredential(trust: t))
        }
        completionHandler(.performDefaultHandling, nil)
    }

    func webView(_ w: WKWebView, runJavaScriptAlertPanelWithMessage msg: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping () -> Void) {
        let a = NSAlert()
        a.messageText = msg
        a.addButton(withTitle: "OK")
        a.beginSheetModal(for: window) { _ in completionHandler() }
    }

    func webView(_ w: WKWebView, runJavaScriptConfirmPanelWithMessage msg: String, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping (Bool) -> Void)
    {
        let a = NSAlert()
        a.messageText = msg
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn) }
    }

    func webView(
        _ w: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String, defaultText: String?, initiatedByFrame f: WKFrameInfo,
        completionHandler: @escaping (String?) -> Void
    ) {
        let a = NSAlert()
        a.messageText = prompt
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 280, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.window.initialFirstResponder = field
        a.beginSheetModal(for: window) { r in completionHandler(r == .alertFirstButtonReturn ? field.stringValue : nil) }
    }

    func webView(_ w: WKWebView, runOpenPanelWith params: WKOpenPanelParameters, initiatedByFrame f: WKFrameInfo, completionHandler: @escaping ([URL]?) -> Void)
    {
        let p = NSOpenPanel()
        p.allowsMultipleSelection = params.allowsMultipleSelection
        p.canChooseDirectories = params.allowsDirectories
        p.beginSheetModal(for: window) { r in completionHandler(r == .OK ? p.urls : nil) }
    }

    // ---------------------------------------------------------------- app life
    func applicationShouldHandleReopen(_ app: NSApplication, hasVisibleWindows: Bool) -> Bool {
        if !hasVisibleWindows { window.makeKeyAndOrderFront(nil) }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { players.isEmpty }

    @objc func reload() { web.reload() }
    @objc func openInBrowser() { NSWorkspace.shared.open(keyed(web.url ?? uiURL)) }
    @objc func zoomIn() { web.pageZoom = min(web.pageZoom + 0.1, 2) }
    @objc func zoomOut() { web.pageZoom = max(web.pageZoom - 0.1, 0.5) }
    @objc func zoomReset() { web.pageZoom = 1 }
    @objc func goHome() { web.load(URLRequest(url: keyed(uiURL))) }

    @objc func stopAndQuit() {
        let a = NSAlert()
        a.messageText = "Stop dlus completely?"
        a.informativeText =
            "Downloads pause and TVs can no longer play from this Mac until you open dlus again. (Quit only closes this window; dlus keeps working in the background.)"
        a.addButton(withTitle: "Stop dlus")
        a.addButton(withTitle: "Cancel")
        guard a.runModal() == .alertFirstButtonReturn else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/bin/launchctl")
        p.arguments = ["bootout", "gui/\(getuid())/com.dlus"]
        try? p.run()
        p.waitUntilExit()
        NSApp.terminate(nil)
    }

    func buildMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) {
            let it = NSMenuItem()
            let m = NSMenu(title: title)
            for item in items { m.addItem(item) }
            it.submenu = m
            main.addItem(it)
        }
        func item(_ t: String, _ a: Selector?, _ k: String = "", _ mods: NSEvent.ModifierFlags = .command, target: AnyObject? = nil) -> NSMenuItem {
            let i = NSMenuItem(title: t, action: a, keyEquivalent: k)
            i.keyEquivalentModifierMask = mods
            if let target = target { i.target = target }
            return i
        }
        sub(
            "dlus",
            [
                item("About dlus", #selector(NSApplication.orderFrontStandardAboutPanel(_:))), .separator(),
                item("Settings...", #selector(openSettings), ",", target: self), .separator(),
                item("Hide dlus", #selector(NSApplication.hide(_:)), "h"),
                item("Hide Others", #selector(NSApplication.hideOtherApplications(_:)), "h", [.command, .option]),
                item("Show All", #selector(NSApplication.unhideAllApplications(_:))), .separator(),
                item("Stop dlus Completely...", #selector(stopAndQuit), target: self),
                item("Quit dlus", #selector(NSApplication.terminate(_:)), "q"),
            ])
        sub(
            "File",
            [
                item("New Download...", #selector(newDownload), "n", target: self), .separator(),
                item("Close Window", #selector(NSWindow.performClose(_:)), "w"),
            ])
        sub(
            "Edit",
            [
                item("Undo", Selector(("undo:")), "z"), item("Redo", Selector(("redo:")), "z", [.command, .shift]), .separator(),
                item("Cut", #selector(NSText.cut(_:)), "x"), item("Copy", #selector(NSText.copy(_:)), "c"),
                item("Paste", #selector(NSText.paste(_:)), "v"), item("Select All", #selector(NSText.selectAll(_:)), "a"), .separator(),
                item("Find in dlus", #selector(findInDlus), "f", target: self),
            ])
        let views: [(String, String)] = [
            ("All Downloads", "all"), ("Active", "active"), ("Queue", "queued"), ("Finished", "done"), ("Library", "lib"),
            ("Network & Storage", "net"), ("Devices & Viewers", "dev"), ("Sharing", "share"), ("VPN Router", "gw"), ("TV & Remote", "tv"),
        ]
        sub(
            "Go",
            views.enumerated().map { i, v in
                let it = item(v.0, #selector(goView(_:)), String((i + 1) % 10), target: self)
                it.representedObject = v.1
                return it
            })
        sub(
            "View",
            [
                item("Reload", #selector(reload), "r", target: self), item("Home", #selector(goHome), "h", [.command, .shift], target: self), .separator(),
                item("Actual Size", #selector(zoomReset), "0", target: self), item("Zoom In", #selector(zoomIn), "+", target: self),
                item("Zoom Out", #selector(zoomOut), "-", target: self), .separator(),
                item("Open in Browser", #selector(openInBrowser), "b", [.command, .option], target: self), .separator(),
                item("Enter Full Screen", #selector(NSWindow.toggleFullScreen(_:)), "f", [.command, .control]),
            ])
        sub("Window", [item("Minimize", #selector(NSWindow.performMiniaturize(_:)), "m"), item("Zoom", #selector(NSWindow.performZoom(_:)))])
        NSApp.mainMenu = main
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.setActivationPolicy(.regular)
app.run()
