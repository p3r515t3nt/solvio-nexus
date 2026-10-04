import SwiftUI
import UIKit
import XCTest
@testable import SolvioApprovals

/// Actual SwiftUI rendering with synthetic chat data in an unpaired simulator.
/// No microphone, transport credential, or production connection is involved.
@MainActor
final class ChatLayoutTests: XCTestCase {
    func testAssistantShellInBothSystemAppearances() async throws {
        for (name, style) in [("light", UIUserInterfaceStyle.light), ("dark", UIUserInterfaceStyle.dark)] {
            let app = AppModel.visualPreview()
            let controller = UIHostingController(rootView: SolvioTabs(model: app))
            controller.overrideUserInterfaceStyle = style
            let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(x: 0, y: 0, width: 390, height: 844)
            window.overrideUserInterfaceStyle = style
            window.windowLevel = .alert + 1
            window.rootViewController = controller; window.makeKeyAndVisible()
            controller.view.frame = window.bounds
            try await Task.sleep(nanoseconds: 400_000_000)
            controller.view.layoutIfNeeded()
            XCTAssertEqual(controller.traitCollection.userInterfaceStyle, style)
            XCTAssertFalse(descendants(controller.view).compactMap { $0 as? UITextView }.isEmpty)
            attach(window, name: "assistant-shell-\(name)")
            window.isHidden = true
        }
    }

    func testPresenceWithVoiceFocusAndLargeType() async throws {
        let chats = ConversationModel()
        let chatID = "c-0123456789abcdef"
        chats.select(chatID, remember: false)
        let raw: [String: Any] = [
            "conversation": ["conversation_id": chatID, "title": "Gemeinsamer Chat", "kind": "text"],
            "messages": [
                ["message_id": "m-0000000000000001", "sequence": 1, "role": "user",
                 "text": "Was könnte ich heute Abend kochen?", "created_at": 1],
                ["message_id": "m-0000000000000002", "sequence": 2, "role": "assistant",
                 "text": "Drei einfache Ideen:\n\n**Pasta mit Pesto** – Nudeln kochen und mit Pesto mischen.\n\n**Gemüse-Omelett** – Gemüse anbraten und mit Eiern stocken lassen.\n\n**Ofenkartoffeln** – Kartoffeln backen, mit Quark servieren.", "created_at": 2]],
            "auftraege": [], "deliveries_open": 0]
        let detail = try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
        await chats.refreshDetail { _ in detail }
        let other = try JSONDecoder().decode(ConversationSummary.self, from: Data("""
            {"conversation_id":"c-2222222222222222","title":"Anderer Chat","kind":"text","open_task_count":1}
            """.utf8))
        await chats.refreshList { [detail.conversation, other] }
        for (name, size, type, presence) in [
            ("idle-background", CGSize(width: 390, height: 844), DynamicTypeSize.large,
             ChatPresencePresentation(state: .idle, title: "SOLVIO ist bereit")),
            ("text-work", CGSize(width: 390, height: 844), .large,
             .init(state: .thinking, title: "SOLVIO bearbeitet deine Nachricht")),
            ("speaking", CGSize(width: 390, height: 844), .large,
             .voice(state: .speaking, status: "SOLVIO spricht", audioLevel: 0.65, microphoneActive: true)),
            ("small-listening", CGSize(width: 320, height: 568), .large,
             .voice(state: .listening, status: "SOLVIO hört zu", audioLevel: 0.3, microphoneActive: true)),
            ("accessible-muted", CGSize(width: 390, height: 844), .accessibility3,
             .voice(state: .listening, status: "Stummgeschaltet", audioLevel: 0.6, microphoneActive: false))] {
            var home = HomeView(model: AppModel.visualPreview(), open: { _ in }, chats: chats)
            home.previewPresence = presence
            let controller = UIHostingController(rootView:
                NavigationStack { home }
                    .environment(\.dynamicTypeSize, type))
            let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(origin: .zero, size: size); window.windowLevel = .alert + 1
            window.rootViewController = controller; window.makeKeyAndVisible()
            controller.view.frame = window.bounds
            try await Task.sleep(nanoseconds: 500_000_000)
            controller.view.layoutIfNeeded()
            let inputs = descendants(controller.view).compactMap { $0 as? UITextView }
            if presence.voiceActive {
                XCTAssertTrue(inputs.isEmpty, "Focused speech has no live text composer")
            } else {
                let input = try XCTUnwrap(inputs.first)
                XCTAssertTrue(input.isEditable)
                let frame = input.convert(input.bounds, to: window)
                XCTAssertGreaterThan(frame.width, 40)
                XCTAssertGreaterThanOrEqual(frame.minY, 0)
                XCTAssertLessThanOrEqual(frame.maxY, size.height)
            }
            attach(window, name: "presence-\(name)")
            window.isHidden = true
        }
        XCTAssertEqual(chats.detail?.messages, detail.messages)
        XCTAssertEqual(chats.selectedID, chatID)
    }

    func testReadableAssistantAndLiteralUserAtSmallAndAccessibleSizes() async throws {
        let chats = ConversationModel()
        let chatID = "c-0123456789abcdef"
        chats.select(chatID, remember: false)
        let raw: [String: Any] = [
            "conversation": ["conversation_id": chatID, "title": "Formatierung", "kind": "text"],
            "messages": [
                ["message_id": "m-0000000000000001", "sequence": 1, "role": "user",
                 "text": "Bitte **wörtlich** und `a_b` erhalten.", "created_at": 1],
                ["message_id": "m-0000000000000002", "sequence": 2, "role": "assistant",
                 "text": "- **Pasta mit Pesto** – Nudeln kochen.\n- **Gemüse-Omelett** – *sanft* braten.\n\nCode: `min(5, 10)`\n[Quelle](https://example.invalid)", "created_at": 2]],
            "auftraege": [], "deliveries_open": 0]
        let detail = try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
        await chats.refreshDetail { _ in detail }
        for (name, width, type) in [
            ("small", 320.0, DynamicTypeSize.large),
            ("regular", 390.0, DynamicTypeSize.large),
            ("accessibility", 390.0, DynamicTypeSize.accessibility3)] {
            let renderer = ImageRenderer(content:
                ConversationView(app: AppModel.visualPreview(), model: chats, conversationID: chatID)
                    .padding(12).frame(width: width).background(Theme.bg)
                    .environment(\.dynamicTypeSize, type))
            renderer.scale = 2
            let image = try XCTUnwrap(renderer.uiImage)
            XCTAssertEqual(image.size.width, width)
            XCTAssertGreaterThan(image.size.height, 100)
            let attachment = XCTAttachment(image: image)
            attachment.name = "chat-readable-\(name)"; attachment.lifetime = .keepAlways
            add(attachment)
        }
        XCTAssertEqual(chats.detail?.messages, detail.messages, "Rendering must not rewrite the canonical transcript")
    }

    func testCompactChatAndKeyboardAtSmallAndAccessibleSizes() async throws {
        let chats = ConversationModel()
        chats.select("c-0123456789abcdef", remember: false)
        let text = String(repeating: "Das ist eine längere synthetische Antwort zur Prüfung des Chatverlaufs. ", count: 18)
        let raw: [String: Any] = [
            "conversation": ["conversation_id": "c-0123456789abcdef", "title": "Mein Testchat", "kind": "text"],
            "messages": [
                ["message_id": "m-0000000000000001", "sequence": 1, "role": "user", "text": "Zeige mir eine ausführliche Antwort.", "created_at": 1],
                ["message_id": "m-0000000000000002", "sequence": 2, "role": "assistant", "text": text, "created_at": 2]],
            "auftraege": [], "deliveries_open": 0]
        let detail = try JSONDecoder().decode(ConversationDetail.self, from: JSONSerialization.data(withJSONObject: raw))
        await chats.refreshDetail { _ in detail }
        for (name, size, type) in [
            ("small", CGSize(width: 320, height: 568), DynamicTypeSize.large),
            ("regular", CGSize(width: 390, height: 844), DynamicTypeSize.large),
            ("accessibility", CGSize(width: 390, height: 844), DynamicTypeSize.accessibility3)] {
            let app = AppModel.visualPreview()
            let controller = UIHostingController(rootView:
                NavigationStack { HomeView(model: app, open: { _ in }, chats: chats) }
                    .environment(\.dynamicTypeSize, type))
            let scene = try XCTUnwrap(UIApplication.shared.connectedScenes.first as? UIWindowScene)
            let window = UIWindow(windowScene: scene)
            window.frame = CGRect(origin: .zero, size: size)
            window.windowLevel = .alert + 1
            window.rootViewController = controller; window.makeKeyAndVisible()
            controller.view.frame = window.bounds
            try await Task.sleep(nanoseconds: 350_000_000)
            controller.view.layoutIfNeeded()
            let inputs = descendants(controller.view).compactMap { $0 as? UITextView }
            let input = try XCTUnwrap(inputs.first, "the actual multiline composer must exist")
            let frame = input.convert(input.bounds, to: window)
            XCTAssertGreaterThan(frame.width, 40)
            XCTAssertGreaterThan(frame.minY, size.height / 2, "composer stays at the bottom instead of after the long transcript")
            XCTAssertLessThanOrEqual(frame.maxY, size.height)
            attach(window, name: "chat-\(name)")
            if name == "small" {
                XCTAssertTrue(input.becomeFirstResponder())
                try await Task.sleep(nanoseconds: 400_000_000)
                controller.view.layoutIfNeeded()
                attach(window, name: "chat-small-keyboard")
                input.resignFirstResponder()
            }
            window.isHidden = true
        }
    }

    private func descendants(_ view: UIView) -> [UIView] {
        view.subviews.flatMap { [$0] + descendants($0) }
    }
    private func attach(_ view: UIView, name: String) {
        let image = UIGraphicsImageRenderer(bounds: view.bounds).image { context in view.layer.render(in: context.cgContext) }
        let attachment = XCTAttachment(image: image); attachment.name = name; attachment.lifetime = .keepAlways
        add(attachment)
    }
}
