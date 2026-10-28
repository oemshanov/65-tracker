// ttcollect — лёгкий фоновый сборщик активности для Time Tracker.
// Раз в N секунд снимает: активное приложение, заголовок окна, URL вкладки браузера,
// простой, блокировку экрана и какой процесс держит микрофон. Пишет «спаны» в SQLite:
// одинаковые подряд идущие состояния склеиваются, поэтому база растёт медленно.
import AppKit
import ApplicationServices
import CoreAudio
import EventKit
import Foundation
import IOKit
import SQLite3
import WebKit

let supportDir = FileManager.default.homeDirectoryForCurrentUser
    .appendingPathComponent("Library/Application Support/TimeTracker")

// MARK: - Конфиг

struct Config {
    var interval: Double = 10
    var idleThreshold: Double = 300
    var browserURLs = true
    var calendar = true
    var dayStartHour = 4
    var ignoreBundles: Set<String> = []
    var redactPatterns: [NSRegularExpression] = []
    var micIgnoreBundles: Set<String> = [
        "com.apple.corespeechd", "com.apple.SpeechRecognitionCore", "com.apple.siri",
        "com.apple.assistantd", "com.apple.controlcenter",
    ]

    static func load() -> Config {
        var c = Config()
        let url = supportDir.appendingPathComponent("config.json")
        guard let data = try? Data(contentsOf: url),
              let root = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return c }
        if let day = root["day"] as? [String: Any], let h = day["start_hour"] as? Int { c.dayStartHour = h }
        guard let col = root["collector"] as? [String: Any] else { return c }
        if let v = col["interval_sec"] as? Double { c.interval = max(2, v) }
        if let v = col["idle_threshold_sec"] as? Double { c.idleThreshold = v }
        if let v = col["browser_urls"] as? Bool { c.browserURLs = v }
        if let v = col["calendar"] as? Bool { c.calendar = v }
        if let v = col["ignore_bundles"] as? [String] { c.ignoreBundles = Set(v) }
        if let v = col["mic_ignore_bundles"] as? [String] { c.micIgnoreBundles.formUnion(v) }
        if let v = col["redact_title_patterns"] as? [String] {
            c.redactPatterns = v.compactMap { try? NSRegularExpression(pattern: $0, options: [.caseInsensitive]) }
        }
        return c
    }
}

// MARK: - SQLite

final class DB {
    var db: OpaquePointer?
    let SQLITE_TRANSIENT = unsafeBitCast(-1, to: sqlite3_destructor_type.self)

    init(path: String) {
        guard sqlite3_open(path, &db) == SQLITE_OK else { fatalError("cannot open db \(path)") }
        exec("PRAGMA journal_mode=WAL")
        exec("PRAGMA busy_timeout=5000")
        exec("""
        CREATE TABLE IF NOT EXISTS spans(
          id INTEGER PRIMARY KEY,
          start REAL NOT NULL, end REAL NOT NULL,
          state TEXT NOT NULL,
          app TEXT, bundle TEXT, title TEXT, url TEXT,
          mic_app TEXT
        )
        """)
        exec("CREATE INDEX IF NOT EXISTS spans_start ON spans(start)")
        exec("""
        CREATE TABLE IF NOT EXISTS calendar_events(
          uid TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL,
          title TEXT, calendar TEXT, location TEXT, all_day INTEGER, attendees INTEGER,
          PRIMARY KEY(uid, start)
        )
        """)
    }

    func exec(_ sql: String) {
        if sqlite3_exec(db, sql, nil, nil, nil) != SQLITE_OK {
            log("sqlite: \(String(cString: sqlite3_errmsg(db))) in \(sql.prefix(60))")
        }
    }

    func scalar(_ sql: String, _ args: [Double]) -> Double {
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else { return 0 }
        defer { sqlite3_finalize(st) }
        for (i, a) in args.enumerated() { sqlite3_bind_double(st, Int32(i + 1), a) }
        return sqlite3_step(st) == SQLITE_ROW ? sqlite3_column_double(st, 0) : 0
    }

    func run(_ sql: String, _ args: [Any?]) -> Int64 {
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK else {
            log("sqlite prepare: \(String(cString: sqlite3_errmsg(db)))"); return -1
        }
        defer { sqlite3_finalize(st) }
        for (i, a) in args.enumerated() {
            let idx = Int32(i + 1)
            switch a {
            case let v as Double: sqlite3_bind_double(st, idx, v)
            case let v as Int: sqlite3_bind_int64(st, idx, Int64(v))
            case let v as Int64: sqlite3_bind_int64(st, idx, v)
            case let v as String: sqlite3_bind_text(st, idx, v, -1, SQLITE_TRANSIENT)
            default: sqlite3_bind_null(st, idx)
            }
        }
        if sqlite3_step(st) != SQLITE_DONE { log("sqlite step: \(String(cString: sqlite3_errmsg(db)))") }
        return sqlite3_last_insert_rowid(db)
    }
}

func log(_ s: String) {
    let f = ISO8601DateFormatter()
    FileHandle.standardError.write("[\(f.string(from: Date()))] \(s)\n".data(using: .utf8)!)
}

// MARK: - Сигналы

/// Секунды с последнего ввода (клавиатура/мышь/трекпад). Разрешений не требует.
func idleSeconds() -> Double {
    let svc = IOServiceGetMatchingService(kIOMainPortDefault, IOServiceMatching("IOHIDSystem"))
    defer { IOObjectRelease(svc) }
    guard let prop = IORegistryEntryCreateCFProperty(svc, "HIDIdleTime" as CFString, kCFAllocatorDefault, 0)?
        .takeRetainedValue() as? NSNumber else { return 0 }
    return prop.doubleValue / 1_000_000_000
}

func screenLocked() -> Bool {
    guard let d = CGSessionCopyCurrentDictionary() as? [String: Any] else { return false }
    return (d["CGSSessionScreenIsLocked"] as? Bool) ?? false
}

/// Заголовок фокусного окна через Accessibility (нужно разрешение «Универсальный доступ»).
func windowTitle(pid: pid_t) -> String? {
    guard AXIsProcessTrusted() else { return nil }
    let app = AXUIElementCreateApplication(pid)
    AXUIElementSetMessagingTimeout(app, 0.5)
    var win: CFTypeRef?
    guard AXUIElementCopyAttributeValue(app, kAXFocusedWindowAttribute as CFString, &win) == .success,
          let w = win else { return nil }
    var title: CFTypeRef?
    guard AXUIElementCopyAttributeValue(w as! AXUIElement, kAXTitleAttribute as CFString, &title) == .success
    else { return nil }
    let s = (title as? String)?.trimmingCharacters(in: .whitespacesAndNewlines)
    return (s?.isEmpty ?? true) ? nil : s
}

let chromiumBrowsers: Set<String> = [
    "com.google.Chrome", "com.google.Chrome.beta", "com.brave.Browser", "com.microsoft.edgemac",
    "company.thebrowser.Browser", "com.vivaldi.Vivaldi", "ru.yandex.desktop.yandex-browser",
    "com.operasoftware.Opera",
]
var scriptCache: [String: NSAppleScript] = [:]

/// URL активной вкладки (нужно разрешение «Автоматизация» для конкретного браузера).
func browserURL(bundle: String) -> String? {
    let src: String
    if chromiumBrowsers.contains(bundle) {
        src = "tell application id \"\(bundle)\" to if (count of windows) > 0 then get URL of active tab of front window"
    } else if bundle == "com.apple.Safari" {
        src = "tell application id \"com.apple.Safari\" to if (count of documents) > 0 then get URL of front document"
    } else { return nil }
    let script = scriptCache[bundle] ?? NSAppleScript(source: src)!
    scriptCache[bundle] = script
    var err: NSDictionary?
    let out = script.executeAndReturnError(&err)
    if err != nil { return nil }
    return out.stringValue
}

/// Bundle ID процесса, который сейчас пишет с микрофона (macOS 14+, без разрешений).
/// Нужен для распознавания созвонов: Zoom/Meet/Telegram держат микрофон.
func micApp(ignore: Set<String>) -> String? {
    guard #available(macOS 14.0, *) else { return nil }
    var addr = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    let sys = AudioObjectID(kAudioObjectSystemObject)
    guard AudioObjectGetPropertyDataSize(sys, &addr, 0, nil, &size) == noErr, size > 0 else { return nil }
    var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    guard AudioObjectGetPropertyData(sys, &addr, 0, nil, &size, &ids) == noErr else { return nil }
    let me = ProcessInfo.processInfo.processIdentifier
    for id in ids {
        var running: UInt32 = 0
        var rsize = UInt32(MemoryLayout<UInt32>.size)
        var raddr = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyIsRunningInput,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        guard AudioObjectGetPropertyData(id, &raddr, 0, nil, &rsize, &running) == noErr, running != 0 else { continue }
        var pid: pid_t = 0
        var psize = UInt32(MemoryLayout<pid_t>.size)
        var paddr = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyPID,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        _ = AudioObjectGetPropertyData(id, &paddr, 0, nil, &psize, &pid)
        if pid == me { continue }
        var bid: Unmanaged<CFString>?
        var bsize = UInt32(MemoryLayout<CFString?>.size)
        var baddr = AudioObjectPropertyAddress(
            mSelector: kAudioProcessPropertyBundleID,
            mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
        var bundle = ""
        if AudioObjectGetPropertyData(id, &baddr, 0, nil, &bsize, &bid) == noErr, let b = bid {
            bundle = b.takeRetainedValue() as String
        }
        if bundle.isEmpty, let app = NSRunningApplication(processIdentifier: pid) {
            bundle = app.bundleIdentifier ?? app.localizedName ?? ""
        }
        if bundle.isEmpty || ignore.contains(bundle) { continue }
        return bundle
    }
    return nil
}

// MARK: - Календарь

final class CalendarSync {
    let store = EKEventStore()
    var granted = false
    var lastSync = Date.distantPast

    init(enabled: Bool) {
        guard enabled else { return }
        if #available(macOS 14.0, *) {
            store.requestFullAccessToEvents { ok, _ in self.granted = ok }
        } else {
            store.requestAccess(to: .event) { ok, _ in self.granted = ok }
        }
    }

    func syncIfNeeded(db: DB) {
        guard granted, Date().timeIntervalSince(lastSync) > 900 else { return }
        lastSync = Date()
        let cal = Calendar.current
        let from = cal.date(byAdding: .day, value: -3, to: cal.startOfDay(for: Date()))!
        let to = cal.date(byAdding: .day, value: 2, to: cal.startOfDay(for: Date()))!
        let events = store.events(matching: store.predicateForEvents(withStart: from, end: to, calendars: nil))
        db.exec("BEGIN")
        _ = db.run("DELETE FROM calendar_events WHERE start >= ? AND start < ?",
                   [from.timeIntervalSince1970, to.timeIntervalSince1970])
        for e in events {
            _ = db.run("INSERT OR REPLACE INTO calendar_events VALUES(?,?,?,?,?,?,?,?)", [
                e.calendarItemExternalIdentifier ?? e.eventIdentifier ?? UUID().uuidString,
                e.startDate.timeIntervalSince1970, e.endDate.timeIntervalSince1970,
                e.title, e.calendar?.title, e.location, e.isAllDay ? 1 : 0, e.attendees?.count ?? 0,
            ])
        }
        db.exec("COMMIT")
    }
}

// MARK: - Цикл сбора

struct Sample: Equatable {
    var state: String  // active | idle | call | locked
    var app: String?
    var bundle: String?
    var title: String?
    var url: String?
    var micApp: String?
}

final class Collector {
    let cfg: Config
    let db: DB
    let calendar: CalendarSync
    var last: Sample?
    var lastID: Int64 = -1
    var lastEnd: Double = 0
    var lastURLBundle: String?
    var lastURLTitle: String?
    var lastURLTime: Double = 0
    var lastURL: String?

    init() {
        cfg = Config.load()
        try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
        db = DB(path: supportDir.appendingPathComponent("tt.db").path)
        calendar = CalendarSync(enabled: cfg.calendar)
    }

    func redact(_ s: String?) -> String? {
        guard let s = s else { return nil }
        let r = NSRange(s.startIndex..., in: s)
        for p in cfg.redactPatterns where p.firstMatch(in: s, range: r) != nil { return "[скрыто]" }
        return s
    }

    func sample() -> (Sample, Double) {
        let idle = idleSeconds()
        let mic = micApp(ignore: cfg.micIgnoreBundles)
        var s = Sample(state: "active")
        if screenLocked() && mic == nil { return (Sample(state: "locked"), 0) }
        if let front = NSWorkspace.shared.frontmostApplication {
            s.bundle = front.bundleIdentifier
            s.app = front.localizedName
            if let b = s.bundle, cfg.ignoreBundles.contains(b) {
                s.app = "[скрыто]"; s.bundle = nil
            } else {
                s.title = redact(windowTitle(pid: front.processIdentifier))
                if cfg.browserURLs, let b = s.bundle {
                    // AppleScript дергаем только при смене вкладки или раз в 30 с.
                    let now = Date().timeIntervalSince1970
                    if b != lastURLBundle || s.title != lastURLTitle || now - lastURLTime > 30 {
                        lastURL = browserURL(bundle: b)
                        lastURLBundle = b; lastURLTitle = s.title; lastURLTime = now
                    }
                    if chromiumBrowsers.contains(b) || b == "com.apple.Safari" {
                        s.url = s.title == "[скрыто]" ? nil : lastURL
                    }
                }
            }
        }
        s.micApp = mic
        if mic != nil {
            s.state = "call"
        } else if idle >= cfg.idleThreshold {
            return (Sample(state: "idle"), idle)
        }
        return (s, idle)
    }

    func tick() {
        let now = Date().timeIntervalSince1970
        if Pause.shared.active {
            // На паузе ничего не пишем; после неё начнётся новый спан.
            last = nil
            lastEnd = 0
            return
        }
        let (s, idle) = sample()
        let continuous = now - lastEnd <= cfg.interval * 3
        if let l = last, l == s, continuous {
            _ = db.run("UPDATE spans SET end=? WHERE id=?", [now, lastID])
        } else {
            var start = continuous ? lastEnd : now
            if s.state == "idle", continuous, last?.state != "idle" {
                // Простой начался idle секунд назад — обрезаем предыдущий активный спан.
                let idleStart = max(now - idle, lastStartOfLast)
                _ = db.run("UPDATE spans SET end=? WHERE id=?", [idleStart, lastID])
                start = idleStart
            }
            lastID = db.run(
                "INSERT INTO spans(start,end,state,app,bundle,title,url,mic_app) VALUES(?,?,?,?,?,?,?,?)",
                [start, now, s.state, s.app, s.bundle, s.title, s.url, s.micApp])
            lastStartOfLast = start
            last = s
        }
        lastEnd = now
        calendar.syncIfNeeded(db: db)
    }
    var lastStartOfLast: Double = 0
}

// MARK: - Пауза

final class Pause {
    static let shared = Pause()
    private let file = supportDir.appendingPathComponent("paused_until")
    /// nil — запись идёт; .infinity — пока не включат; иначе — до момента.
    private(set) var until: Double?

    init() {
        if let s = try? String(contentsOf: file, encoding: .utf8),
           let v = Double(s.trimmingCharacters(in: .whitespacesAndNewlines)) {
            until = v < 0 ? .infinity : v
        }
    }

    /// Файл паузы пишет и панель (через сервер таймлайна) — перечитываем перед каждой проверкой.
    func reload() {
        if let s = try? String(contentsOf: file, encoding: .utf8),
           let v = Double(s.trimmingCharacters(in: .whitespacesAndNewlines)) {
            until = v < 0 ? .infinity : v
        } else {
            until = nil
        }
    }

    var active: Bool {
        reload()
        guard let u = until else { return false }
        if Date().timeIntervalSince1970 >= u { set(nil); return false }
        return true
    }

    func set(_ u: Double?) {
        until = u
        if let u = u {
            try? (u.isInfinite ? "-1" : String(u)).write(to: file, atomically: true, encoding: .utf8)
        } else {
            try? FileManager.default.removeItem(at: file)
        }
    }
}

// MARK: - Строка меню

let ttScript = supportDir.appendingPathComponent("app/tt.py").path

// MARK: - Приложение из .dmg: свой Python и код трекера внутри

let resourcesDir = Bundle.main.resourcePath ?? ""
/// Python внутри приложения (сборка build_dmg.sh); у установки через install.sh — системный.
let pythonExe: String = {
    let b = resourcesDir + "/python/bin/python3"
    return FileManager.default.isExecutableFile(atPath: b) ? b : "/usr/bin/python3"
}()

/// Первый запуск без терминала: кладём код трекера в папку данных (если там другая версия),
/// записываем, где лежат приложение и Python, ставим агенты launchd (автозапуск и напоминание).
func bootstrap() {
    let fm = FileManager.default
    if pythonExe != "/usr/bin/python3" {  // байт-код Python — в папку данных, а не внутрь приложения (иначе слетит подпись)
        setenv("PYTHONPYCACHEPREFIX", supportDir.appendingPathComponent("pycache").path, 1)
    }
    try? fm.createDirectory(at: supportDir.appendingPathComponent("logs"), withIntermediateDirectories: true)
    try? Bundle.main.bundlePath.write(to: supportDir.appendingPathComponent("app_path"), atomically: true, encoding: .utf8)
    try? pythonExe.write(to: supportDir.appendingPathComponent("python_path"), atomically: true, encoding: .utf8)
    let res = URL(fileURLWithPath: resourcesDir), src = res.appendingPathComponent("app")
    guard fm.fileExists(atPath: src.appendingPathComponent("tt.py").path) else { return }  // код ставит install.sh
    let dst = supportDir.appendingPathComponent("app")
    let stamp = { (u: URL) in (try? String(contentsOf: u.appendingPathComponent(".build"), encoding: .utf8)) ?? "" }
    if stamp(src).isEmpty || stamp(src) != stamp(dst) {
        try? fm.removeItem(at: dst)
        try? fm.copyItem(at: src, to: dst)
        log("Код трекера разложен из приложения: \(stamp(src).trimmingCharacters(in: .whitespacesAndNewlines))")
    }
    let cfg = supportDir.appendingPathComponent("config.json"), example = res.appendingPathComponent("config.example.json")
    if !fm.fileExists(atPath: cfg.path), fm.fileExists(atPath: example.path) { try? fm.copyItem(at: example, to: cfg) }
    // Агенты ставим, только если их нет или приложение переехало: иначе launchd перезапустил бы работающий сборщик.
    let plist = fm.homeDirectoryForCurrentUser.appendingPathComponent("Library/LaunchAgents/com.timetracker.collector.plist")
    var current = ""
    if let d = try? Data(contentsOf: plist),
       let p = (try? PropertyListSerialization.propertyList(from: d, format: nil)) as? [String: Any],
       let args = p["ProgramArguments"] as? [String] { current = args.first ?? "" }
    if current != Bundle.main.executablePath {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: pythonExe)
        p.arguments = [ttScript, "agents", "install"]
        p.standardOutput = FileHandle.nullDevice
        p.standardError = FileHandle.nullDevice
        try? p.run()
        log("Агенты launchd установлены для \(Bundle.main.bundlePath)")
    }
    installSkill(res)
}

/// Скилл /timesheet для Claude Code и команда tt — только если Claude Code стоит.
/// Чужую (install.sh) команду tt не трогаем: свою узнаём по пометке.
func installSkill(_ res: URL) {
    let fm = FileManager.default, home = fm.homeDirectoryForCurrentUser
    guard fm.fileExists(atPath: home.appendingPathComponent(".claude").path) else { return }
    let skill = res.appendingPathComponent("skills/timesheet"), to = home.appendingPathComponent(".claude/skills/timesheet")
    if fm.fileExists(atPath: skill.path) {
        try? fm.createDirectory(at: to.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? fm.removeItem(at: to)
        try? fm.copyItem(at: skill, to: to)
    }
    let bin = home.appendingPathComponent(".local/bin"), tt = bin.appendingPathComponent("tt")
    let mine = "# 65 tracker"
    let old = (try? String(contentsOf: tt, encoding: .utf8)) ?? ""
    guard old.isEmpty || old.contains(mine) else { return }
    try? fm.createDirectory(at: bin, withIntermediateDirectories: true)
    try? "#!/bin/sh\n\(mine)\nexec '\(pythonExe)' '\(ttScript)' \"$@\"\n".write(to: tt, atomically: true, encoding: .utf8)
    chmod(tt.path, 0o755)
}

func runTT(_ args: [String]) {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: pythonExe)
    p.arguments = [ttScript] + args
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    try? p.run()
}

/// Открывает Терминал с командой — для одноразовых интерактивных шагов (ввод токена Jira).
func runInTerminal(_ command: String) {
    let url = FileManager.default.temporaryDirectory.appendingPathComponent("tt-\(UUID().uuidString).command")
    let body = "#!/bin/zsh\nclear\n\(command)\necho\nread '?Готово. Нажмите Enter, чтобы закрыть окно.'\nrm -f \"$0\"\n"
    try? body.write(to: url, atomically: true, encoding: .utf8)
    chmod(url.path, 0o755)
    NSWorkspace.shared.open(url)
}

func jiraConfigured() -> Bool {
    let p = Process()
    p.executableURL = URL(fileURLWithPath: "/usr/bin/security")
    p.arguments = ["find-generic-password", "-s", "tt-jira"]
    p.standardOutput = FileHandle.nullDevice
    p.standardError = FileHandle.nullDevice
    guard (try? p.run()) != nil else { return false }
    p.waitUntilExit()
    return p.terminationStatus == 0
}

/// Знак 65 для строки меню (шаблонное изображение — macOS сама красит его под светлую/тёмную тему).
let badge65: NSImage? = {
    let svg = "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 168 165' width='17' height='17'><path fill-rule='evenodd' fill='#000' d='M95.4827 0.432335L29.9467 17.8621C24.3692 19.4889 18.7917 25.0664 17.3973 30.4115L0.432335 93.8559C-0.962046 99.4334 1.12953 107.103 5.08027 111.053L53.6512 159.159C57.602 163.11 65.2711 165.202 70.8486 163.807L136.385 146.378C141.73 144.983 147.54 139.406 148.934 133.828L165.899 70.3838C167.293 64.8063 165.202 57.1372 161.251 53.1864L112.68 5.08028C108.729 1.12953 101.06 -0.962047 95.4827 0.432335ZM25.7632 82.9323C24.3688 85.9535 23.4392 89.4394 23.4392 93.855C23.4392 98.0381 24.1364 101.756 25.7632 105.01C27.1576 108.264 29.0168 111.285 31.8055 113.609C34.5943 115.933 37.6155 117.792 41.3338 119.186C45.0522 120.581 49.0029 121.278 53.4185 121.278C57.834 121.278 61.7847 120.581 65.5031 119.186C69.2215 117.792 72.475 115.933 75.0314 113.609C77.5877 111.285 79.6793 108.264 81.0737 105.01C82.4681 101.756 83.1653 98.0381 83.1653 93.855C83.1653 89.9042 82.4681 86.1859 81.0737 82.9323C79.6793 79.6788 77.5877 76.6576 75.0314 74.3336C72.475 72.0097 69.4538 70.1505 65.7355 68.7561C62.2495 67.3617 58.2988 66.6645 53.8832 66.6645L67.3623 46.9108C67.5947 46.9108 67.8271 46.6784 67.8271 46.446C68.0595 46.2136 68.0595 45.9812 68.0595 45.5164C68.0595 44.8192 67.8271 44.122 67.1299 43.4248C66.4327 42.7277 65.7355 42.4953 65.0383 42.4953L55.2776 42.4953C54.1156 42.4953 53.4185 42.7277 52.9537 43.1924L52.9536 43.1925C52.4889 43.6573 52.0241 44.122 51.7917 44.5868L30.8759 74.3336C29.0168 77.1224 27.1576 79.9112 25.7632 82.9323ZM64.1087 84.5591C66.6651 87.1155 68.0595 90.369 68.0595 94.0874C68.0595 97.8057 66.6651 101.059 64.1087 103.848C61.5523 106.404 57.834 107.799 53.1861 107.799C48.5381 107.799 44.8198 106.404 42.2634 103.848C39.707 101.059 38.3126 97.8057 38.3126 94.0874C38.3126 90.369 39.707 87.1155 42.2634 84.5591C44.8198 82.0027 48.5381 80.6084 53.1861 80.6084C57.834 80.6084 61.5523 82.0027 64.1087 84.5591ZM92.4611 43.1924C91.7639 43.6572 91.5316 44.3544 91.5316 45.284L88.5104 82.4675C88.5104 83.1647 88.7428 83.8619 89.44 84.5591C90.1372 85.2563 90.8344 85.4887 91.5316 85.4887H101.757C102.919 85.4887 103.616 85.2563 104.313 85.0239C104.778 84.7915 105.708 84.3267 106.405 83.8619C107.102 83.3971 108.032 82.9323 109.426 82.6999C110.588 82.4675 112.447 82.2351 114.539 82.2351C118.722 82.2351 122.208 83.3971 124.764 85.7211C127.321 88.0451 128.715 90.8338 128.715 94.0874C128.715 98.2705 127.321 101.292 124.764 103.616C121.976 105.94 118.722 107.102 114.539 107.102H114.539C112.912 107.102 111.285 107.102 109.659 106.869C108.032 106.637 106.637 106.172 105.475 105.475C104.313 104.778 103.151 104.08 102.222 102.918C101.292 101.756 100.595 100.362 100.13 98.7353C99.8978 98.0381 99.6655 97.3409 98.9683 96.8761C98.2711 96.4113 97.5739 96.1789 96.8767 96.1789H88.0456C87.3484 96.1789 86.6512 96.6437 85.954 97.1085C85.2568 97.5733 85.0244 98.2705 85.0244 99.2001C85.2568 102.221 85.954 105.01 87.116 107.566C88.278 110.123 90.1372 112.447 92.4611 114.538C94.7851 116.63 97.8063 118.257 101.525 119.419C105.243 120.581 109.426 121.278 114.539 121.278C118.49 121.278 122.208 121.045 125.694 119.883C129.18 118.721 132.201 117.095 134.757 115.003C137.314 112.912 139.173 109.89 140.8 106.637C142.426 103.383 143.124 99.2001 143.124 94.5522C143.124 90.6014 142.426 86.8831 141.032 83.6295C139.638 80.376 137.546 77.5872 134.99 75.4956C132.433 73.4041 129.412 71.7773 125.926 70.6153C122.44 69.4533 118.722 68.9885 114.539 68.9885C113.144 68.9885 111.518 68.9885 109.659 69.2209C107.799 69.2209 105.94 69.6857 103.849 70.6153L105.011 56.2067H135.919C136.617 56.2067 137.314 55.9743 138.011 55.2771C138.708 54.8123 138.941 54.1151 138.941 53.1855V45.284C138.941 44.5868 138.708 43.8896 138.011 43.1924C137.314 42.4953 136.617 42.2629 135.919 42.2629H94.5527C93.8555 42.2629 93.1583 42.4953 92.4611 43.1924Z'/></svg>"
    let img = NSImage(data: Data(svg.utf8))
    img?.isTemplate = true
    return img
}()

func fmtDur(_ sec: Double) -> String {
    let m = Int((sec / 60).rounded())
    let h = m / 60, r = m % 60
    if h > 0 && r > 0 { return "\(h)ч \(String(format: "%02d", r))м" }
    return h > 0 ? "\(h)ч" : "\(r)м"
}

// MARK: - Панель и окно таймлайна (страницы локального сервера в WKWebView)

let serverURL = "http://127.0.0.1:8765/"

/// Поднимает сервер таймлайна (tt.py ensure-server) и отдаёт его адрес — в фоне, UI не ждёт.
func ensureServer(_ done: @escaping (String) -> Void) {
    DispatchQueue.global(qos: .userInitiated).async {
        let p = Process(), out = Pipe()
        p.executableURL = URL(fileURLWithPath: pythonExe)
        p.arguments = [ttScript, "ensure-server"]
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        var url = serverURL
        if (try? p.run()) != nil {
            p.waitUntilExit()
            let s = String(data: out.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            if s.hasPrefix("http") { url = s }
        }
        DispatchQueue.main.async { done(url) }
    }
}

/// Ссылки наружу (Jira, Ollama) — в браузер; внутри остаются только страницы трекера.
/// Верхняя кромка окна: тянешь — окно едет, двойной клик — развернуть, как у заголовка.
final class DragStrip: NSView {
    override func mouseDown(with event: NSEvent) {
        if event.clickCount == 2 { window?.performZoom(nil) } else { window?.performDrag(with: event) }
    }
}

// MARK: - Доступы

/// Каких разрешений не хватает — пишем в permissions.json, страницы показывают по нему баннер.
func writePermissions() {
    var cal = "ok"
    switch EKEventStore.authorizationStatus(for: .event) {
    case .notDetermined: cal = "ask"
    case .denied, .restricted, .writeOnly: cal = "denied"
    default: cal = "ok"
    }
    var browsers: [[String: Any]] = []
    var seen = Set<String>()
    for app in NSWorkspace.shared.runningApplications {
        guard let bid = app.bundleIdentifier, chromiumBrowsers.contains(bid) || bid == "com.apple.Safari",
              !seen.contains(bid) else { continue }
        seen.insert(bid)
        let st = automationStatus(bid, ask: false)
        let state = st == noErr ? "ok" : st == OSStatus(errAEEventWouldRequireUserConsent) ? "ask" : st == -600 ? "ok" : "denied"
        browsers.append(["bundle": bid, "name": app.localizedName ?? bid, "state": state])
    }
    let o: [String: Any] = ["accessibility": AXIsProcessTrusted(), "calendar": cal, "browsers": browsers,
                            "updated": Date().timeIntervalSince1970]
    if let d = try? JSONSerialization.data(withJSONObject: o) {
        try? d.write(to: supportDir.appendingPathComponent("permissions.json"), options: .atomic)
    }
}

func automationStatus(_ bundle: String, ask: Bool) -> OSStatus {
    let t = NSAppleEventDescriptor(bundleIdentifier: bundle)
    return AEDeterminePermissionToAutomateTarget(t.aeDesc, typeWildCard, typeWildCard, ask)
}

func openPrivacy(_ pane: String) {
    NSWorkspace.shared.open(URL(string: "x-apple.systempreferences:com.apple.preference.security?" + pane)!)
}

final class WebDelegate: NSObject, WKNavigationDelegate, WKUIDelegate {
    func webView(_ webView: WKWebView, decidePolicyFor action: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        if let u = action.request.url, u.host != "127.0.0.1", u.scheme == "http" || u.scheme == "https" {
            NSWorkspace.shared.open(u)
            return decisionHandler(.cancel)
        }
        decisionHandler(.allow)
    }

    func webView(_ webView: WKWebView, createWebViewWith configuration: WKWebViewConfiguration,
                 for action: WKNavigationAction, windowFeatures: WKWindowFeatures) -> WKWebView? {
        if let u = action.request.url {
            if u.host == "127.0.0.1" { webView.load(URLRequest(url: u)) } else { NSWorkspace.shared.open(u) }
        }
        return nil
    }
}

final class MenuController: NSObject, NSMenuDelegate, NSApplicationDelegate, WKScriptMessageHandler, NSWindowDelegate {
    let collector: Collector
    let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    let menu = NSMenu()
    let popover = NSPopover()
    let webDelegate = WebDelegate()
    var panelWeb: WKWebView?
    var window: NSWindow?
    var windowWeb: WKWebView?
    var showTime: Bool {
        get { UserDefaults.standard.object(forKey: "showTime") as? Bool ?? true }
        set { UserDefaults.standard.set(newValue, forKey: "showTime") }
    }

    init(collector: Collector) {
        self.collector = collector
        super.init()
        menu.delegate = self
        menu.autoenablesItems = false
        // Левый клик — панель, правый — прежнее меню.
        item.button?.target = self
        item.button?.action = #selector(statusClick(_:))
        item.button?.sendAction(on: [.leftMouseUp, .rightMouseUp])
        popover.behavior = .transient
        popover.contentSize = NSSize(width: 380, height: 600)
        popover.contentViewController = NSViewController()
        refreshIcon()
        let t = Timer(timeInterval: 60, repeats: true) { [weak self] _ in self?.refreshIcon() }
        RunLoop.main.add(t, forMode: .common)
        buildMainMenu()
    }

    /// Меню вверху экрана, пока открыто окно: «65 tracker» (о программе, настройки, выйти), «Правка» — без неё
    /// в полях окна не работают ⌘C/⌘V, «Окно».
    func buildMainMenu() {
        let main = NSMenu()
        func sub(_ title: String, _ items: [NSMenuItem]) -> NSMenu {
            let it = NSMenuItem(), m = NSMenu(title: title)
            items.forEach { m.addItem($0) }
            it.submenu = m
            main.addItem(it)
            return m
        }
        func mi(_ title: String, _ sel: Selector, _ key: String = "", mods: NSEvent.ModifierFlags = .command,
                own: Bool = false) -> NSMenuItem {
            let it = NSMenuItem(title: title, action: sel, keyEquivalent: key)
            it.keyEquivalentModifierMask = mods
            if own { it.target = self }
            return it
        }
        _ = sub("65 tracker", [
            mi("О программе 65 tracker", #selector(about), own: true), .separator(),
            mi("Настройки…", #selector(openSettings), ",", own: true), .separator(),
            mi("Скрыть 65 tracker", #selector(NSApplication.hide(_:)), "h"), .separator(),
            mi("Выйти из 65 tracker", #selector(quit), "q", own: true),
        ])
        _ = sub("Правка", [
            mi("Отменить", Selector(("undo:")), "z"), mi("Повторить", Selector(("redo:")), "z", mods: [.command, .shift]),
            .separator(),
            mi("Вырезать", #selector(NSText.cut(_:)), "x"), mi("Скопировать", #selector(NSText.copy(_:)), "c"),
            mi("Вставить", #selector(NSText.paste(_:)), "v"), mi("Выбрать всё", #selector(NSText.selectAll(_:)), "a"),
        ])
        let wm = sub("Окно", [
            mi("Свернуть", #selector(NSWindow.performMiniaturize(_:)), "m"),
            mi("Закрыть", #selector(NSWindow.performClose(_:)), "w"), .separator(),
            mi("Обновить", #selector(reloadWindow), "r", own: true),
        ])
        NSApp.mainMenu = main
        NSApp.windowsMenu = wm
    }

    @objc func about() {
        NSApp.activate(ignoringOtherApps: true)
        NSApp.orderFrontStandardAboutPanel(options: [.applicationName: "65 tracker"])
    }

    @objc func reloadWindow() { windowWeb?.reload() }

    func dayStart(_ offsetDays: Int = 0) -> Date {
        let cal = Calendar.current
        let shifted = Date().addingTimeInterval(-Double(collector.cfg.dayStartHour) * 3600)
        let d = cal.date(byAdding: .day, value: offsetDays, to: cal.startOfDay(for: shifted))!
        return d.addingTimeInterval(Double(collector.cfg.dayStartHour) * 3600)
    }

    func activeSeconds(from: Date, to: Date) -> Double {
        let a = from.timeIntervalSince1970, b = to.timeIntervalSince1970
        return collector.db.scalar(
            "SELECT COALESCE(SUM(MIN(end, ?) - MAX(start, ?)), 0) FROM spans " +
            "WHERE state IN ('active','call') AND start < ? AND end > ?", [b, a, b, a])
    }

    func refreshIcon() {
        writePermissions()
        guard let b = item.button else { return }
        let paused = Pause.shared.active, trusted = AXIsProcessTrusted()
        b.image = badge65 ?? NSImage(systemSymbolName: "record.circle", accessibilityDescription: "65 tracker")
        b.imagePosition = .imageLeading
        b.appearsDisabled = paused
        if !trusted {
            b.title = " нет доступа"  // без «Универсального доступа» не видны заголовки окон
        } else if showTime && !paused {
            let s = activeSeconds(from: dayStart(), to: dayStart(1))
            let m = Int(s / 60)
            b.title = " \(m / 60):\(String(format: "%02d", m % 60))"
        } else {
            b.title = paused ? " пауза" : ""
        }
        b.toolTip = !trusted ? "65 tracker: нет доступа к окнам — включите в Настройки → Конфиденциальность → Универсальный доступ"
            : paused ? "65 tracker: пауза" : "65 tracker: запись идёт"
    }

    func add(_ title: String, _ action: Selector?, key: String = "", obj: Any? = nil, enabled: Bool = true,
             to m: NSMenu? = nil) -> NSMenuItem {
        let it = NSMenuItem(title: title, action: action, keyEquivalent: key)
        it.target = self
        it.representedObject = obj
        it.isEnabled = enabled && action != nil
        (m ?? menu).addItem(it)
        return it
    }

    func menuWillOpen(_ menu: NSMenu) {
        menu.removeAllItems()
        let paused = Pause.shared.active
        let today = activeSeconds(from: dayStart(), to: dayStart(1))

        if paused {
            let u = Pause.shared.until!
            let when = u.isInfinite ? "пока не включите" : "до " + DateFormatter.localizedString(
                from: Date(timeIntervalSince1970: u), dateStyle: .none, timeStyle: .short)
            _ = add("⏸  Пауза — \(when)", nil, enabled: false)
            _ = add("Продолжить запись", #selector(resume), key: "r")
        } else {
            _ = add("●  Запись идёт · сегодня \(fmtDur(today))", nil, enabled: false)
            let pm = NSMenu()
            for (title, mins) in [("На 30 минут", 30), ("На 1 час", 60), ("На 2 часа", 120)] {
                _ = add(title, #selector(pause(_:)), obj: Double(mins * 60), to: pm)
            }
            _ = add("До конца дня", #selector(pause(_:)), obj: -2.0, to: pm)
            _ = add("Пока не включу", #selector(pause(_:)), obj: -1.0, to: pm)
            let pi = add("Пауза", #selector(noop))
            pi.submenu = pm
        }
        if !AXIsProcessTrusted() {
            menu.addItem(.separator())
            _ = add("⚠︎ Нет доступа к окнам — включить…", #selector(openAccessibility))
        }

        menu.addItem(.separator())
        _ = add("Открыть таймлайн", #selector(openDay(_:)), key: "o", obj: 0)
        _ = add("Вчера", #selector(openDay(_:)), obj: -1)
        let dm = NSMenu()
        let fmt = DateFormatter()
        fmt.locale = Locale(identifier: "ru_RU")
        fmt.dateFormat = "EEEEEE, d MMMM"
        for i in 0..<14 {
            let s = activeSeconds(from: dayStart(-i), to: dayStart(-i + 1))
            if s < 60 && i > 0 { continue }
            _ = add("\(fmt.string(from: dayStart(-i)))  —  \(fmtDur(s))", #selector(openDay(_:)), obj: -i, to: dm)
        }
        let di = add("Последние дни", #selector(noop))
        di.submenu = dm

        menu.addItem(.separator())
        if jiraConfigured() {
            _ = add("Jira подключена", nil, enabled: false)
            _ = add("Обновить задачи из Jira", #selector(syncIssues))
        } else {
            _ = add("Подключить Jira…", #selector(setupJira))
        }
        _ = add("Настройки…", #selector(openSettings), key: ",")
        let st = add("Время в строке меню", #selector(toggleTime))
        st.state = showTime ? .on : .off

        menu.addItem(.separator())
        _ = add("Выйти из Учёта времени", #selector(quit), key: "q")
    }

    func webView(frame: NSRect) -> WKWebView {
        let cfg = WKWebViewConfiguration()
        cfg.userContentController.add(self, name: "tt")
        let w = WKWebView(frame: frame, configuration: cfg)
        w.navigationDelegate = webDelegate
        w.uiDelegate = webDelegate
        w.setValue(false, forKey: "drawsBackground")
        return w
    }

    @objc func statusClick(_ sender: Any?) {
        if NSApp.currentEvent?.type == .rightMouseUp || NSApp.currentEvent?.modifierFlags.contains(.control) == true {
            popover.performClose(nil)
            item.menu = menu
            item.button?.performClick(nil)  // откроет меню; в menuDidClose отвязываем, чтобы левый клик снова был панелью
            return
        }
        togglePanel()
    }

    func menuDidClose(_ menu: NSMenu) { item.menu = nil }

    func togglePanel() {
        if popover.isShown { popover.performClose(nil); return }
        guard let button = item.button else { return }
        if panelWeb == nil {
            let w = webView(frame: NSRect(origin: .zero, size: popover.contentSize))
            popover.contentViewController?.view = w
            panelWeb = w
            ensureServer { url in w.load(URLRequest(url: URL(string: url + "widget")!)) }
        } else {
            writePermissions()
            panelWeb?.evaluateJavaScript("window.ttRefresh && window.ttRefresh()")
        }
        popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// Мост из страниц: open (день в окне таймлайна), settings, quit, refresh (сменилась пауза).
    func userContentController(_ uc: WKUserContentController, didReceive message: WKScriptMessage) {
        guard let body = message.body as? [String: Any], let action = body["action"] as? String else { return }
        switch action {
        case "open":
            popover.performClose(nil)
            openTimeline(date: body["date"] as? String)
        case "settings":
            popover.performClose(nil)
            openSettings()
        case "quit":
            quit()
        case "refresh":
            refreshIcon()
        case "accessibility":
            popover.performClose(nil)
            openAccessibility()
        case "calendar":
            popover.performClose(nil)
            if EKEventStore.authorizationStatus(for: .event) == .notDetermined {
                EKEventStore().requestFullAccessToEvents { _, _ in DispatchQueue.main.async { self.refreshIcon() } }
            } else { openPrivacy("Privacy_Calendars") }
        case "automation":
            popover.performClose(nil)
            let bid = body["bundle"] as? String ?? "com.google.Chrome"
            DispatchQueue.global().async {
                let st = automationStatus(bid, ask: true)  // спросит macOS, если ещё не спрашивала
                DispatchQueue.main.async {
                    if st != noErr { openPrivacy("Privacy_Automation") }
                    self.refreshIcon()
                }
            }
        default:
            break
        }
    }

    /// Таймлайн — своё окно приложения, а не вкладка браузера.
    func openTimeline(date: String?, hash: String = "") {
        let q = (date.map { "?date=" + $0 } ?? "") + hash
        if window == nil {
            // Окно Mac без отдельной полосы заголовка: страница идёт до самого верха, кнопки окна поверх неё,
            // а таскать окно можно за верхнюю кромку (прозрачная полоса над страницей).
            let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1360, height: 900),
                             styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
                             backing: .buffered, defer: false)
            w.title = "65 tracker"
            w.titlebarAppearsTransparent = true
            w.titleVisibility = .hidden
            // Пустая панель в стиле unified делает полосу заголовка выше (~52 pt): кнопки окна встают по её центру,
            // и шапка страницы (логотип, аккаунт) не прилипает к верхнему краю. Фон панели прозрачный — видна страница.
            let bar = NSToolbar(identifier: "timeline")
            bar.showsBaselineSeparator = false
            w.toolbar = bar
            w.toolbarStyle = .unified
            w.backgroundColor = NSColor(name: nil) { a in
                a.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
                    ? NSColor(srgbRed: 0.059, green: 0.059, blue: 0.063, alpha: 1)
                    : NSColor(srgbRed: 0.925, green: 0.925, blue: 0.918, alpha: 1)
            }
            w.minSize = NSSize(width: 1000, height: 640)
            w.isReleasedWhenClosed = false
            w.delegate = self
            w.setFrameAutosaveName("TimelineWindow")
            if w.frame.origin == .zero { w.center() }
            let root = NSView(frame: w.contentView!.bounds)
            let web = webView(frame: root.bounds)
            web.autoresizingMask = [.width, .height]
            root.addSubview(web)
            let strip = DragStrip(frame: NSRect(x: 0, y: root.bounds.height - 52, width: root.bounds.width - 260, height: 52))
            strip.autoresizingMask = [.width, .minYMargin]
            root.addSubview(strip)
            w.contentView = root
            window = w
            windowWeb = web
        }
        ensureServer { [weak self] url in self?.windowWeb?.load(URLRequest(url: URL(string: url + q)!)) }
        showWindow()
    }

    /// Пока окно открыто — приложение в Dock, в ⌘Tab и со своим меню вверху экрана, как обычное.
    /// macOS не отдаёт строку меню приложению, которое только что было «фоновым»: на миг активируем Dock,
    /// затем себя — тогда вверху появляется «65 tracker», а не меню предыдущего приложения.
    func showWindow() {
        let wasAccessory = NSApp.activationPolicy() != .regular
        NSApp.setActivationPolicy(.regular)
        window?.makeKeyAndOrderFront(nil)
        if wasAccessory, let dock = NSRunningApplication.runningApplications(withBundleIdentifier: "com.apple.dock").first {
            dock.activate()
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.08) {
                NSApp.activate(ignoringOtherApps: true)
                self.window?.makeKeyAndOrderFront(nil)
            }
        } else {
            NSApp.activate(ignoringOtherApps: true)
        }
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
    }

    @objc func noop() {}

    @objc func pause(_ sender: NSMenuItem) {
        let v = sender.representedObject as! Double
        let now = Date().timeIntervalSince1970
        if v == -1 { Pause.shared.set(.infinity) }
        else if v == -2 { Pause.shared.set(dayStart(1).timeIntervalSince1970) }
        else { Pause.shared.set(now + v) }
        refreshIcon()
    }

    @objc func resume() {
        Pause.shared.set(nil)
        collector.tick()
        refreshIcon()
    }

    @objc func openDay(_ sender: NSMenuItem) {
        let off = sender.representedObject as! Int
        let f = DateFormatter()
        f.dateFormat = "yyyy-MM-dd"
        openTimeline(date: f.string(from: dayStart(off)))
    }

    @objc func openAccessibility() {
        requestAccessibility()
        openPrivacy("Privacy_Accessibility")
        refreshIcon()
    }

    @objc func setupJira() {
        runInTerminal("'\(pythonExe)' '\(ttScript)' setup")
    }

    @objc func syncIssues() { runTT(["issues", "--sync"]) }

    /// Настройки — окно в самом таймлайне (Jira, напоминание, доступы, версия), а не config.json в TextEdit.
    @objc func openSettings() {
        if window != nil, windowWeb?.url != nil {
            showWindow()
            windowWeb?.evaluateJavaScript("window.ttSettings && window.ttSettings()")
        } else {
            openTimeline(date: nil, hash: "#settings")
        }
    }

    @objc func toggleTime() { showTime.toggle(); refreshIcon() }

    @objc func quit() {
        // Код 0: launchd не перезапускает. Снова запустить — открыть «65 tracker» через Spotlight.
        NSApp.terminate(nil)
    }

    // Повторный запуск (Spotlight, Finder, Dock) — открываем окно таймлайна.
    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        openTimeline(date: nil)
        return false
    }
}

// MARK: - main

let args = CommandLine.arguments.dropFirst()
if args.first == "--check" {
    let trusted = AXIsProcessTrusted()
    let front = NSWorkspace.shared.frontmostApplication
    let out: [String: Any] = [
        "accessibility": trusted,
        "idle_sec": Int(idleSeconds()),
        "locked": screenLocked(),
        "front_app": front?.localizedName ?? NSNull(),
        "front_bundle": front?.bundleIdentifier ?? NSNull(),
        "title": front.flatMap { windowTitle(pid: $0.processIdentifier) } ?? NSNull(),
        "url": front?.bundleIdentifier.flatMap { browserURL(bundle: $0) } ?? NSNull(),
        "mic_app": micApp(ignore: Config().micIgnoreBundles) ?? NSNull(),
        "paused": Pause.shared.active,
    ]
    let data = try! JSONSerialization.data(withJSONObject: out, options: [.prettyPrinted, .sortedKeys])
    print(String(data: data, encoding: .utf8)!)
    exit(0)
}

/// После пересборки в списке «Универсального доступа» остаётся старая запись (включена, но от прежней подписи),
/// и macOS считает, что уже спрашивала, — системный запрос не появляется. Убираем свою устаревшую запись
/// и спрашиваем заново: появится окно macOS «Открыть Системные настройки», останется включить переключатель.
func requestAccessibility() {
    if let bid = Bundle.main.bundleIdentifier {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/tccutil")
        p.arguments = ["reset", "Accessibility", bid]
        try? p.run()
        p.waitUntilExit()
    }
    _ = AXIsProcessTrustedWithOptions([kAXTrustedCheckOptionPrompt.takeUnretainedValue(): true] as CFDictionary)
}

// Один экземпляр: если уже запущен (launchd или вручную) — выходим. Замок на файле, а не список приложений:
// при первой установке автозапуск поднимает вторую копию почти одновременно, и список её ещё не видит.
try? FileManager.default.createDirectory(at: supportDir, withIntermediateDirectories: true)
let instanceLock = open(supportDir.appendingPathComponent("collector.lock").path, O_CREAT | O_RDWR, 0o644)
if instanceLock < 0 || flock(instanceLock, LOCK_EX | LOCK_NB) != 0 {
    exit(0)
}

bootstrap()

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
if !AXIsProcessTrusted() {
    requestAccessibility()
    log("Нет разрешения «Универсальный доступ» — заголовки окон собираться не будут, пока его не выдать.")
}
let collector = Collector()
log("ttcollect started, interval=\(collector.cfg.interval)s")
let menuController = MenuController(collector: collector)
app.delegate = menuController
collector.tick()
let timer = Timer(timeInterval: collector.cfg.interval, repeats: true) { _ in collector.tick() }
timer.tolerance = collector.cfg.interval * 0.2
RunLoop.main.add(timer, forMode: .common)
signal(SIGHUP) { _ in exit(0) }
app.run()
