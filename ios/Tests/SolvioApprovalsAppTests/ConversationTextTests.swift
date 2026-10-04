import Foundation
import XCTest
@testable import SolvioApprovals

@MainActor
final class ConversationTextTests: XCTestCase {
    private func message(_ text: String, role: String = "assistant") -> ConversationMessage {
        ConversationMessage(message_id: "m-0000000000000001", sequence: 1, role: role,
                            text: text, created_at: 1, delivery: nil)
    }

    func testAssistantUsesNativeEmphasisAndCodeWithoutChangingSource() {
        let original = message("**Pasta**, *sanft* und `min(5, 10)`.")
        let displayed = ConversationView.bubbleText(original)
        XCTAssertEqual(String(displayed.characters), "Pasta, sanft und min(5, 10).")
        for (word, intent) in [("Pasta", InlinePresentationIntent.stronglyEmphasized),
                               ("sanft", .emphasized), ("min(5, 10)", .code)] {
            XCTAssertTrue(displayed.runs.contains { run in
                String(displayed[run.range].characters) == word
                    && run.inlinePresentationIntent?.contains(intent) == true
            })
        }
        XCTAssertEqual(original.text, "**Pasta**, *sanft* und `min(5, 10)`.")
    }

    func testOriginalWhitespaceAndLineBreaksSurviveInlineFormatting() {
        let text = "- **Pasta** – schnell.\n\n  *Gemüse*  dazu.\n\t`a_b`\n"
        let displayed = ConversationView.bubbleText(message(text))
        XCTAssertEqual(String(displayed.characters), "- Pasta – schnell.\n\n  Gemüse  dazu.\n\ta_b\n")
    }

    func testOnlyAssistantTextIsInterpreted() {
        let text = "**wörtlich** *so* `a_b`\n[Quelle](https://example.invalid)"
        for role in ["user", "system", "tool", "unknown"] {
            let displayed = ConversationView.bubbleText(message(text, role: role))
            XCTAssertEqual(String(displayed.characters), text)
            XCTAssertTrue(displayed.runs.allSatisfy { $0.inlinePresentationIntent == nil && $0.link == nil })
        }
    }

    func testMarkdownLinksKeepTheirLabelsWithoutAnyURLAction() {
        for destination in ["https://example.invalid", "http://example.invalid", "mailto:test@example.invalid",
                            "tel:123", "file:///tmp/example", "solvio://task/example", "javascript:alert(1)"] {
            let displayed = ConversationView.bubbleText(message("**Hinweis:** [Quelle](\(destination))"))
            XCTAssertEqual(String(displayed.characters), "Hinweis: Quelle", destination)
            XCTAssertTrue(displayed.runs.allSatisfy { $0.link == nil }, destination)
        }
        let automatic = ConversationView.bubbleText(message("<https://example.invalid>"))
        XCTAssertEqual(String(automatic.characters), "https://example.invalid")
        XCTAssertTrue(automatic.runs.allSatisfy { $0.link == nil })
    }

    func testPlainAndIncompleteTextRemainsReadable() {
        for text in ["", "Grüße 👋 – 2 < 3 & 5 > 4.", "Eine **unfertige Hervorhebung", "Unfertiger `Code"] {
            XCTAssertEqual(String(ConversationView.bubbleText(message(text)).characters), text)
        }
    }
}
