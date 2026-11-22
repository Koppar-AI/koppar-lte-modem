// Koppar Phone: the menu-bar app for the modem.
//
// A status-bar icon (signal, unread texts, calls), a compact panel when you
// click it, the full UI in a window when you expand, and a floating call
// panel when the phone rings. It starts `koppar-lte-modem daemon` if none is
// running and talks to it over the daemon's local API (127.0.0.1, token).

import AppKit
import ServiceManagement
import SwiftUI
import WebKit

// MARK: - Configuration

enum Config {
    static let env = ProcessInfo.processInfo.environment
    static let port = Int(env["KOPPAR_UI_PORT"] ?? "") ?? 8025
    static var base: URL { URL(string: "http://127.0.0.1:\(port)")! }

    static var dataDir: URL {
        if let d = env["KOPPAR_DATA"] { return URL(fileURLWithPath: d) }
        return FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("koppar-lte-modem")
    }

    static func token() -> String? {
        let url = dataDir.appendingPathComponent("ui-token")
        return (try? String(contentsOf: url, encoding: .utf8))?.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The koppar-lte-modem command: KOPPAR_CLI, else the one in the app bundle
    /// (release builds: "@bundle/cli/koppar-lte-modem"), else a path baked in by build.sh.
    static var cli: String? {
        if let c = env["KOPPAR_CLI"] { return c }
        guard let v = Bundle.main.object(forInfoDictionaryKey: "KopparCLI") as? String, !v.isEmpty else { return nil }
        if v.hasPrefix("@bundle/"), let res = Bundle.main.resourceURL {
            return res.appendingPathComponent(String(v.dropFirst("@bundle/".count))).path
        }
        return v
    }
}

let copper = NSColor(srgbRed: 0.72, green: 0.41, blue: 0.24, alpha: 1)

// MARK: - API

enum APIError: LocalizedError {
    case noToken, http(String)
    var errorDescription: String? {
        switch self {
        case .noToken: return "No UI token yet: the daemon hasn't started."
        case .http(let m): return m
        }
    }
}

enum API {
    static let session: URLSession = {
        let c = URLSessionConfiguration.ephemeral
        c.timeoutIntervalForRequest = 120
        c.timeoutIntervalForResource = .infinity
        return URLSession(configuration: c)
    }()

    @discardableResult
    static func call(_ path: String, _ method: String = "GET", _ body: [String: Any]? = nil,
                     timeout: TimeInterval = 20) async throws -> Any {
        guard let tok = Config.token() else { throw APIError.noToken }
        var r = URLRequest(url: URL(string: path, relativeTo: Config.base)!)
        r.httpMethod = method
        r.timeoutInterval = timeout
        r.setValue(tok, forHTTPHeaderField: "X-Koppar-Token")
        if let body {
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        let (data, resp) = try await session.data(for: r)
        let obj = (try? JSONSerialization.jsonObject(with: data)) ?? NSNull()
        if let h = resp as? HTTPURLResponse, !(200..<300).contains(h.statusCode) {
            throw APIError.http((obj as? [String: Any])?["error"] as? String ?? "HTTP \(h.statusCode)")
        }
        return obj
    }
}

// MARK: - Model

struct CallInfo: Equatable {
    var direction: String
    var state: String
    var number: String?
    var name: String?
    var incoming: Bool { direction == "in" && (state == "incoming" || state == "waiting") }
    var who: String { name ?? number ?? "Unknown number" }
}

struct ThreadInfo: Identifiable, Equatable {
    var id: String { number }
    var number: String
    var name: String?
    var lastText: String
    var lastTime: String
    var lastOut: Bool
    var unread: Int
}

@MainActor
final class Model: ObservableObject {
    static let shared = Model()

    @Published var live = false
    @Published var note: String?
    @Published var modem = false
    @Published var sim: String?
    @Published var op: String?
    @Published var tech: String?
    @Published var band: String?
    @Published var registered = false
    @Published var bars = 0
    @Published var rsrp: Double?
    @Published var rssi: Double?
    @Published var call: CallInfo?
    @Published var bridge = false
    @Published var recording = false
    @Published var threads: [ThreadInfo] = []
    @Published var unread = 0
    @Published var voicemailNew = 0
    @Published var operatorVoicemail = false
    @Published var voicemailBusy = false
    @Published var keypad = false
    @Published var atLogin = SMAppService.mainApp.status == .enabled
    var callStart: Date?

    func apply(state s: [String: Any]) {
        modem = s["modem"] as? Bool ?? false
        sim = s["sim"] as? String
        let net = s["network"] as? [String: Any] ?? [:]
        op = net["operator"] as? String
        tech = net["tech"] as? String
        band = net["band"] as? String
        registered = net["registered"] as? Bool ?? false
        applySignal(s["signal"] as? [String: Any] ?? [:])
        bridge = s["bridge"] as? Bool ?? false
        recording = s["recording"] as? Bool ?? false
        unread = s["unread"] as? Int ?? unread
        voicemailNew = s["voicemail_new"] as? Int ?? 0
        operatorVoicemail = (s["voicemail_waiting"] as? [String: Any])?["active"] as? Bool ?? false
        voicemailBusy = s["voicemail_busy"] as? Bool ?? false
        var c: CallInfo?
        if let calls = s["calls"] as? [[String: Any]], let first = calls.first {
            c = CallInfo(direction: first["direction"] as? String ?? "in", state: first["state"] as? String ?? "",
                         number: first["number"] as? String, name: first["name"] as? String)
        } else if s["ringing"] as? Bool == true {
            c = CallInfo(direction: "in", state: "incoming", number: s["ringing_number"] as? String,
                         name: s["ringing_name"] as? String)
        }
        if c?.state == "active" && callStart == nil { callStart = Date() }
        if c == nil { callStart = nil }
        if c != call { call = c }
    }

    func applySignal(_ g: [String: Any]) {
        bars = g["bars"] as? Int ?? 0
        rsrp = g["rsrp"] as? Double ?? (g["rsrp"] as? Int).map(Double.init)
        rssi = g["rssi"] as? Double ?? (g["rssi"] as? Int).map(Double.init)
    }

    func refresh() async {
        do {
            if let s = try await API.call("/api/state") as? [String: Any] { apply(state: s) }
            if let ts = try await API.call("/api/threads") as? [[String: Any]] {
                threads = ts.map {
                    ThreadInfo(number: $0["number"] as? String ?? "", name: $0["name"] as? String,
                               lastText: $0["last_text"] as? String ?? "", lastTime: $0["last_time"] as? String ?? "",
                               lastOut: ($0["last_direction"] as? String) == "out", unread: $0["unread"] as? Int ?? 0)
                }
                unread = threads.reduce(0) { $0 + $1.unread }
            }
        } catch {
            // the stream reconnects; nothing to do here
        }
    }

    func handle(_ ev: [String: Any]) async {
        let kind = ev["event"] as? String ?? ""
        if ev["ui"] as? Bool == true {
            switch kind {
            case "signal": applySignal(ev)
            case "message", "bridge", "read", "voicemail_heard": await refresh()
            default: break
            }
            return
        }
        let refreshOn: Set = ["ring", "incoming_call", "call_state", "call_ended", "call_audio", "ready", "module_on",
                              "module_off", "network", "modem_disconnected", "sms_sent", "sms", "voicemail",
                              "voicemail_answered", "voicemail_waiting", "voicemail_failed"]
        if refreshOn.contains(kind) { await refresh() }
    }

    func act(_ path: String, _ body: [String: Any] = [:]) {
        Task {
            do { try await API.call(path, "POST", body) } catch { note = error.localizedDescription }
            await refresh()
        }
    }
}

// MARK: - Event stream

final class EventStream {
    func run() async {
        while !Task.isCancelled {
            do { try await connect() } catch {}
            await MainActor.run { Model.shared.live = false }
            try? await Task.sleep(nanoseconds: 2_000_000_000)
        }
    }

    private func connect() async throws {
        guard let tok = Config.token() else { throw APIError.noToken }
        var comps = URLComponents(url: Config.base.appendingPathComponent("api/stream"), resolvingAgainstBaseURL: false)!
        comps.queryItems = [URLQueryItem(name: "token", value: tok)]
        let (bytes, resp) = try await API.session.bytes(for: URLRequest(url: comps.url!))
        guard (resp as? HTTPURLResponse)?.statusCode == 200 else { throw APIError.http("stream refused") }
        await MainActor.run { Model.shared.live = true; Model.shared.note = nil }
        await Model.shared.refresh()
        for try await line in bytes.lines where line.hasPrefix("data: ") {
            guard let d = line.dropFirst(6).data(using: .utf8),
                  let ev = try? JSONSerialization.jsonObject(with: d) as? [String: Any] else { continue }
            await Model.shared.handle(ev)
        }
    }
}

// MARK: - The daemon

@MainActor
final class Daemon {
    private var process: Process?
    private var stopping = false

    func reachable() async -> Bool {
        (try? await API.call("/api/state", timeout: 2)) != nil
    }

    func ensure() async {
        if await reachable() { return }
        guard let cli = Config.cli, FileManager.default.isExecutableFile(atPath: cli) else {
            Model.shared.note = "The daemon isn't running, and the koppar-lte-modem command wasn't found."
            return
        }
        Model.shared.note = "Starting the modem daemon…"
        start(cli)
        for _ in 0..<40 {
            try? await Task.sleep(nanoseconds: 500_000_000)
            if await reachable() { Model.shared.note = nil; return }
        }
        Model.shared.note = "The daemon didn't come up: see daemon.log in the data folder."
    }

    private func start(_ cli: String) {
        try? FileManager.default.createDirectory(at: Config.dataDir, withIntermediateDirectories: true)
        let logURL = Config.dataDir.appendingPathComponent("daemon.log")
        if !FileManager.default.fileExists(atPath: logURL.path) {
            FileManager.default.createFile(atPath: logURL.path, contents: nil)
        }
        let log = try? FileHandle(forWritingTo: logURL)
        log?.seekToEndOfFile()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cli)
        // The app shows its own call panel: no notifications or browser windows from the daemon.
        p.arguments = ["daemon", "--no-popup"]
        p.standardOutput = log
        p.standardError = log
        p.terminationHandler = { [weak self] _ in
            Task { @MainActor in
                guard let self, !self.stopping else { return }
                self.process = nil
                try? await Task.sleep(nanoseconds: 3_000_000_000)
                self.start(cli)                       // it died on its own: start it again
            }
        }
        do { try p.run(); process = p } catch { Model.shared.note = "Couldn't start the daemon: \(error.localizedDescription)" }
    }

    func stop() {
        stopping = true
        guard let p = process, p.isRunning else { return }
        p.terminate()                                 // SIGTERM: it powers nothing off, just lets go
        let deadline = Date().addingTimeInterval(5)
        while p.isRunning && Date() < deadline { usleep(50_000) }
    }
}

// MARK: - Views

struct Bars: View {
    var n: Int
    var body: some View {
        HStack(alignment: .bottom, spacing: 2) {
            ForEach(1...4, id: \.self) { i in
                RoundedRectangle(cornerRadius: 1)
                    .fill(i <= n ? Color(copper) : Color.secondary.opacity(0.25))
                    .frame(width: 4, height: CGFloat(3 + i * 3))
            }
        }
    }
}

struct Avatar: View {
    var name: String?
    var size: CGFloat = 32
    var body: some View {
        ZStack {
            Circle().fill(Color(copper).opacity(0.16))
            if let n = name, let c = n.first, !c.isNumber, c != "+" {
                Text(String(c).uppercased()).font(.system(size: size * 0.42, weight: .semibold)).foregroundColor(Color(copper))
            } else {
                Image(systemName: "person.fill").font(.system(size: size * 0.42)).foregroundColor(Color(copper))
            }
        }.frame(width: size, height: size)
    }
}

func shortTime(_ iso: String) -> String {
    let f = ISO8601DateFormatter()
    guard let d = f.date(from: iso) else { return "" }
    let out = DateFormatter()
    out.dateFormat = Calendar.current.isDateInToday(d) ? "HH:mm" : "d MMM"
    return out.string(from: d)
}

func callLabel(_ c: CallInfo, start: Date?, now: Date) -> String {
    switch c.state {
    case "active":
        guard let s = start else { return "In call" }
        let t = Int(now.timeIntervalSince(s))
        return String(format: "%d:%02d", t / 60, t % 60)
    case "incoming": return "Incoming call"
    case "waiting": return "Call waiting"
    case "dialing": return "Calling…"
    case "alerting": return "Ringing…"
    case "held": return "On hold"
    default: return c.state
    }
}

struct PopoverView: View {
    @ObservedObject var m = Model.shared
    var open: (String) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(spacing: 8) {
                Image(nsImage: NSApp.applicationIconImage).resizable().frame(width: 20, height: 20)
                Text("Koppar Phone").font(.headline)
                Spacer()
                Circle().fill(m.live ? Color.green : Color.orange).frame(width: 7, height: 7)
                Text(m.live ? "Live" : "Connecting…").font(.caption).foregroundColor(.secondary)
            }
            if let note = m.note {
                Text(note).font(.caption).foregroundColor(.orange).fixedSize(horizontal: false, vertical: true)
            }
            HStack(spacing: 10) {
                Bars(n: m.bars)
                VStack(alignment: .leading, spacing: 1) {
                    Text([m.op, m.tech].compactMap { $0 }.joined(separator: " · ").ifEmpty(m.modem ? "Searching…" : "No modem"))
                        .font(.system(size: 13, weight: .medium))
                    Text(signalLine).font(.caption).foregroundColor(.secondary)
                }
                Spacer()
            }
            if let c = m.call {
                HStack(spacing: 10) {
                    Avatar(name: c.who, size: 30)
                    VStack(alignment: .leading, spacing: 1) {
                        Text(c.who).font(.system(size: 13, weight: .semibold)).lineLimit(1)
                        TimelineView(.periodic(from: .now, by: 1)) { ctx in
                            Text(callLabel(c, start: m.callStart, now: ctx.date)).font(.caption).foregroundColor(Color(copper))
                        }
                    }
                    Spacer()
                    if c.incoming {
                        Button("Answer") { m.act("/api/call/answer", ["audio": true]) }.tint(.green)
                    }
                    Button(c.incoming ? "Decline" : "Hang up") { m.act("/api/call/hangup") }.tint(.red)
                }
                .buttonStyle(.borderedProminent).controlSize(.small)
                .padding(8).background(RoundedRectangle(cornerRadius: 8).fill(Color(copper).opacity(0.08)))
            }
            if m.voicemailNew > 0 || m.operatorVoicemail {
                Button { open("voicemail") } label: {
                    HStack(spacing: 8) {
                        Image(systemName: "recordingtape").foregroundColor(Color(copper))
                        Text(m.voicemailNew > 0 ? "\(m.voicemailNew) new voicemail\(m.voicemailNew == 1 ? "" : "s")"
                                                : "New messages on your operator's voicemail")
                            .font(.system(size: 12.5, weight: .semibold))
                        Spacer()
                        Text("Listen").font(.caption).foregroundColor(Color(copper))
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
            Divider()
            HStack {
                Text("Messages").font(.caption).foregroundColor(.secondary)
                if m.unread > 0 {
                    Text("\(m.unread)").font(.caption2.bold()).foregroundColor(.white)
                        .padding(.horizontal, 5).padding(.vertical, 1).background(Capsule().fill(Color(copper)))
                }
                Spacer()
            }
            if m.threads.isEmpty {
                Text("No texts yet.").font(.caption).foregroundColor(.secondary)
            }
            ForEach(Array(m.threads.prefix(4))) { t in
                Button { open("messages:\(t.number)") } label: {
                    HStack(spacing: 8) {
                        Avatar(name: t.name ?? t.number, size: 26)
                        VStack(alignment: .leading, spacing: 0) {
                            HStack {
                                Text(t.name ?? t.number).font(.system(size: 12.5, weight: t.unread > 0 ? .bold : .regular)).lineLimit(1)
                                Spacer()
                                if t.unread > 0 {
                                    Circle().fill(Color(copper)).frame(width: 7, height: 7).accessibilityLabel("unread")
                                }
                                Text(shortTime(t.lastTime)).font(.caption2.weight(t.unread > 0 ? .semibold : .regular))
                                    .foregroundColor(t.unread > 0 ? Color(copper) : .secondary)
                            }
                            Text((t.lastOut ? "You: " : "") + t.lastText).font(.caption.weight(t.unread > 0 ? .medium : .regular))
                                .foregroundColor(t.unread > 0 ? .primary : .secondary).lineLimit(1)
                        }
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain)
            }
            Divider()
            HStack {
                Button { open("messages:__new") } label: { Label("Text", systemImage: "square.and.pencil") }
                Button { open("calls") } label: { Label("Call", systemImage: "phone") }
                Spacer()
                Button("Open Full App") { open("overview") }.keyboardShortcut(.defaultAction)
            }.controlSize(.small)
            HStack {
                Toggle("Open at login", isOn: Binding(get: { m.atLogin }, set: { on in
                    do {
                        if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
                        m.atLogin = on
                    } catch { m.note = "Login item: \(error.localizedDescription)" }
                })).toggleStyle(.checkbox).font(.caption)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.font(.caption).buttonStyle(.link)
            }
        }
        .padding(14)
        .frame(width: 340)
    }

    var signalLine: String {
        var parts: [String] = []
        if let r = m.rsrp { parts.append("RSRP \(Int(r)) dBm") } else if let r = m.rssi { parts.append("\(Int(r)) dBm") }
        if let b = m.band { parts.append(b.replacingOccurrences(of: "LTE BAND ", with: "band ")) }
        parts.append("SIM " + (m.sim ?? "–"))
        return parts.joined(separator: " · ")
    }
}

extension String {
    func ifEmpty(_ other: String) -> String { isEmpty ? other : self }
}

struct CallView: View {
    @ObservedObject var m = Model.shared
    var open: (String) -> Void

    var body: some View {
        if let c = m.call {
            VStack(spacing: 10) {
                HStack(spacing: 12) {
                    Avatar(name: c.who, size: 44)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(c.who).font(.system(size: 16, weight: .semibold)).lineLimit(1)
                        if c.name != nil, let n = c.number { Text(n).font(.caption).foregroundColor(.secondary) }
                        TimelineView(.periodic(from: .now, by: 1)) { ctx in
                            Text(callLabel(c, start: m.callStart, now: ctx.date)).font(.caption.weight(.semibold))
                                .foregroundColor(Color(copper))
                        }
                    }
                    Spacer()
                    Button { open("calls") } label: { Image(systemName: "arrow.up.left.and.arrow.down.right") }
                        .buttonStyle(.borderless).help("Open the full app")
                }
                if m.voicemailBusy {
                    HStack {
                        Label("Voicemail is recording a message", systemImage: "recordingtape").font(.callout)
                        Spacer()
                        Button { m.act("/api/call/hangup") } label: { Image(systemName: "phone.down.fill") }
                            .buttonStyle(.borderedProminent).tint(.red).help("End the call")
                    }
                } else if c.incoming {
                    HStack(spacing: 10) {
                        Button { m.act("/api/call/hangup") } label: { Label("Decline", systemImage: "phone.down.fill").frame(maxWidth: .infinity) }
                            .tint(.red)
                        Button { m.act("/api/call/answer", ["audio": true]) } label: { Label("Answer", systemImage: "phone.fill").frame(maxWidth: .infinity) }
                            .tint(.green).keyboardShortcut(.defaultAction)
                    }.buttonStyle(.borderedProminent).controlSize(.large)
                    Button("Answer without the Mac's audio") { m.act("/api/call/answer", ["audio": false]) }
                        .buttonStyle(.link).font(.caption)
                } else {
                    HStack(spacing: 8) {
                        Toggle(isOn: Binding(get: { m.bridge }, set: { on in m.act("/api/audio/bridge", ["on": on]) })) {
                            Label("Mac audio", systemImage: "mic")
                        }
                        Toggle(isOn: Binding(get: { m.recording }, set: { on in m.act("/api/audio/bridge", ["on": on, "record": on]) })) {
                            Label("Record", systemImage: "record.circle")
                        }
                        Toggle(isOn: $m.keypad) { Image(systemName: "circle.grid.3x3") }.help("Keypad")
                        Spacer()
                        Button { m.act("/api/call/hangup") } label: { Image(systemName: "phone.down.fill") }
                            .buttonStyle(.borderedProminent).tint(.red).help("Hang up")
                    }.toggleStyle(.button).controlSize(.regular)
                    if m.keypad {
                        LazyVGrid(columns: Array(repeating: GridItem(.flexible()), count: 3), spacing: 6) {
                            ForEach(["1", "2", "3", "4", "5", "6", "7", "8", "9", "*", "0", "#"], id: \.self) { k in
                                Button(k) { m.act("/api/call/dtmf", ["digits": k]) }.frame(maxWidth: .infinity)
                            }
                        }.controlSize(.large)
                    }
                }
            }
            .padding(16)
            .frame(width: 320)
        } else {
            EmptyView()
        }
    }
}

// MARK: - The full window

@MainActor
final class MainWindow: NSObject, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate {
    private var window: NSWindow?
    private var web: WKWebView?

    func show(_ view: String) {
        let safe = view.filter { $0.isLetter || $0.isNumber || "+:_".contains($0) }
        if window == nil {
            // A one-time launch code (the master token never goes in a URL); the page
            // swaps it for a session of its own.
            Task { @MainActor in
                do {
                    let r = try await API.call("/api/launch", "POST", [:]) as? [String: Any]
                    guard let code = r?["launch"] as? String,
                          let url = URL(string: "\(Config.base.absoluteString)/?launch=\(code)&embedded=1#\(safe)") else {
                        throw APIError.http("no launch code")
                    }
                    self.open(url)
                } catch {
                    Model.shared.note = "The daemon isn't ready yet: \(error.localizedDescription)"
                }
            }
            return
        }
        web?.evaluateJavaScript("location.hash = '#\(safe)'")
        bringForward()
    }

    private func open(_ url: URL) {
        do {
            if window != nil { return }
            let cfg = WKWebViewConfiguration()
            cfg.websiteDataStore = .nonPersistent()
            let w = WKWebView(frame: .zero, configuration: cfg)
            w.navigationDelegate = self
            w.uiDelegate = self                       // confirm() and prompt() in the page
            w.load(URLRequest(url: url))
            let win = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
                               styleMask: [.titled, .closable, .miniaturizable, .resizable], backing: .buffered, defer: false)
            win.title = "Koppar Phone"
            win.contentView = w
            win.minSize = NSSize(width: 640, height: 480)
            win.isReleasedWhenClosed = false
            win.delegate = self
            win.setFrameAutosaveName("KopparMain")
            if !win.setFrameUsingName("KopparMain") { win.center() }
            window = win
            web = w
        }
        bringForward()
    }

    private func bringForward() {
        NSApp.setActivationPolicy(.regular)           // in the Dock and Cmd-Tab while the window is open
        NSApp.activate(ignoringOtherApps: true)
        window?.makeKeyAndOrderFront(nil)
    }

    var isVisible: Bool { window?.isVisible ?? false }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)         // back to the menu bar only
    }

    // The page's confirm(), prompt() and alert(): native dialogs. A web view without
    // these shows nothing, and confirm() quietly returns false.
    func webView(_ webView: WKWebView, runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo) async {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        _ = await sheet(a)
    }

    func webView(_ webView: WKWebView, runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo) async -> Bool {
        let a = NSAlert()
        a.messageText = message
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        return await sheet(a) == .alertFirstButtonReturn
    }

    func webView(_ webView: WKWebView, runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?, initiatedByFrame frame: WKFrameInfo) async -> String? {
        let a = NSAlert()
        a.messageText = prompt
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 260, height: 24))
        field.stringValue = defaultText ?? ""
        a.accessoryView = field
        a.addButton(withTitle: "OK")
        a.addButton(withTitle: "Cancel")
        a.window.initialFirstResponder = field
        return await sheet(a) == .alertFirstButtonReturn ? field.stringValue : nil
    }

    private func sheet(_ a: NSAlert) async -> NSApplication.ModalResponse {
        guard let w = window else { return a.runModal() }
        return await withCheckedContinuation { c in a.beginSheetModal(for: w) { c.resume(returning: $0) } }
    }

    // The window only ever shows the local UI. A link elsewhere opens in the browser,
    // and only if it's http(s): no file:, custom-scheme or app-opening URLs.
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction) async -> WKNavigationActionPolicy {
        guard let url = action.request.url else { return .cancel }
        if url.scheme == "blob" || (url.scheme == "http" && url.host == "127.0.0.1" && url.port == Config.port) {
            return .allow
        }
        if action.navigationType == .linkActivated, ["http", "https"].contains(url.scheme ?? "") {
            NSWorkspace.shared.open(url)
        }
        return .cancel
    }
}

// MARK: - The call panel

@MainActor
final class CallPanel {
    private var panel: NSPanel?
    private var ringTimer: Timer?
    var open: (String) -> Void = { _ in }

    func update(_ call: CallInfo?) {
        guard let call else { close(); return }
        if panel == nil {
            let host = NSHostingView(rootView: CallView(open: open))
            let p = NSPanel(contentRect: NSRect(x: 0, y: 0, width: 320, height: 150),
                            styleMask: [.titled, .closable, .nonactivatingPanel, .utilityWindow, .hudWindow],
                            backing: .buffered, defer: false)
            p.title = "Call"
            p.contentView = host
            p.level = .floating
            p.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
            p.isFloatingPanel = true
            p.hidesOnDeactivate = false
            p.isReleasedWhenClosed = false
            if let screen = NSScreen.main {
                let f = screen.visibleFrame
                p.setFrameTopLeftPoint(NSPoint(x: f.maxX - 340, y: f.maxY - 12))
            }
            panel = p
        }
        panel?.setContentSize(panel?.contentView?.fittingSize ?? NSSize(width: 320, height: 150))
        panel?.orderFrontRegardless()
        ring(call.incoming)
    }

    private func ring(_ on: Bool) {
        if on && ringTimer == nil {
            NSSound(named: "Glass")?.play()
            ringTimer = Timer.scheduledTimer(withTimeInterval: 2.2, repeats: true) { _ in NSSound(named: "Glass")?.play() }
        } else if !on {
            ringTimer?.invalidate()
            ringTimer = nil
        }
    }

    func close() {
        ring(false)
        panel?.orderOut(nil)
        panel = nil
    }
}

// MARK: - App

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private let popover = NSPopover()
    private let main = MainWindow()
    private let callPanel = CallPanel()
    private let daemon = Daemon()
    private var streamTask: Task<Void, Never>?
    private var observer: Any?
    private var blink = false
    private var blinkTimer: Timer?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        NSApp.mainMenu = mainMenu()

        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.target = self
        item.button?.action = #selector(togglePopover)
        item.button?.imagePosition = .imageLeading

        popover.behavior = .transient
        popover.contentViewController = NSHostingController(rootView: PopoverView(open: { [weak self] v in self?.open(v) }))

        callPanel.open = { [weak self] v in self?.open(v) }
        observer = Model.shared.objectWillChange.sink { [weak self] _ in
            DispatchQueue.main.async { self?.render() }
        }
        render()

        Task {
            await daemon.ensure()
            streamTask = Task.detached { await EventStream().run() }
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        streamTask?.cancel()
        daemon.stop()
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        open("overview")
        return true
    }

    @objc private func togglePopover() {
        guard let b = item.button else { return }
        if popover.isShown { popover.performClose(nil) } else {
            Task { await Model.shared.refresh() }
            popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
            popover.contentViewController?.view.window?.makeKey()
        }
    }

    private func open(_ view: String) {
        popover.performClose(nil)
        main.show(view)
    }

    private func render() {
        let m = Model.shared
        callPanel.update(m.call)
        guard let b = item.button else { return }
        if let c = m.call {
            let img = NSImage(systemSymbolName: c.incoming ? "phone.fill.arrow.down.left" : "phone.fill",
                              accessibilityDescription: "Call")!
            img.isTemplate = !c.incoming
            b.image = img
            b.contentTintColor = c.incoming ? copper : nil
            b.title = ""
            if c.incoming && blinkTimer == nil {
                blinkTimer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
                    Task { @MainActor in
                        guard let self else { return }
                        self.blink.toggle()
                        self.item.button?.alphaValue = self.blink ? 0.35 : 1
                    }
                }
            }
        } else {
            b.image = barsImage(m.live && m.modem ? m.bars : -1)
            b.contentTintColor = nil
            b.title = m.unread > 0 ? " \(m.unread)" : ""
        }
        if !(m.call?.incoming ?? false) {
            blinkTimer?.invalidate()
            blinkTimer = nil
            b.alphaValue = 1
        }
        b.toolTip = m.live ? "Koppar Phone: \([m.op, m.tech].compactMap { $0 }.joined(separator: " "))" : "Koppar Phone: connecting…"
    }

    /// Four signal bars as a template image; n = -1 draws them hollow (no modem).
    private func barsImage(_ n: Int) -> NSImage {
        let img = NSImage(size: NSSize(width: 18, height: 14), flipped: false) { _ in
            for i in 0..<4 {
                let h = CGFloat(4 + i * 3)
                let r = NSRect(x: CGFloat(1 + i * 4), y: 1, width: 3, height: h)
                let path = NSBezierPath(roundedRect: r, xRadius: 0.8, yRadius: 0.8)
                if n >= 0 && i < n {
                    NSColor.black.setFill()
                    path.fill()
                } else {
                    NSColor.black.withAlphaComponent(n < 0 ? 0.5 : 0.25).setFill()
                    path.fill()
                }
            }
            return true
        }
        img.isTemplate = true
        img.accessibilityDescription = "Koppar Phone signal"
        return img
    }

    private func mainMenu() -> NSMenu {
        let menu = NSMenu()
        let app = NSMenu()
        app.addItem(withTitle: "About Koppar Phone", action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)), keyEquivalent: "")
        app.addItem(.separator())
        app.addItem(withTitle: "Hide Koppar Phone", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        app.addItem(withTitle: "Quit Koppar Phone", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        let win = NSMenu(title: "Window")
        win.addItem(withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        win.addItem(withTitle: "Minimize", action: #selector(NSWindow.performMiniaturize(_:)), keyEquivalent: "m")
        for (title, sub) in [("Koppar Phone", app), ("Edit", edit), ("Window", win)] {
            let it = NSMenuItem(title: title, action: nil, keyEquivalent: "")
            it.submenu = sub
            menu.addItem(it)
        }
        return menu
    }
}

@main
struct KopparApp {
    static func main() {
        let app = NSApplication.shared
        let delegate = MainActor.assumeIsolated { AppDelegate() }
        app.delegate = delegate
        app.run()
    }
}
