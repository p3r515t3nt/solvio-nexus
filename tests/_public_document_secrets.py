"""Independent secret-shape check for the public documentation subset."""
from pathlib import Path
import re
from _guard import require_equal

SECRETS = [('\\bsk-[A-Za-z0-9_-]{20,}', 'OpenAI-artiger Schluessel'), ('-----BEGIN [A-Z ]*PRIVATE KEY-----', 'privater Schluessel'), ('\\bghp_[A-Za-z0-9]{20,}', 'GitHub-Token'), ('\\bxox[baprs]-[A-Za-z0-9-]{10,}', 'Slack-Token'), ('\\bAKIA[0-9A-Z]{16}\\b', 'AWS-Zugriffsschluessel'), ('(?i)\\b(password|passwort|secret|token|api[_-]?key)\\s*[:=]\\s*(?!//)[\\"\']?[^\\s\\"\'<>{}$]{8,}', 'zugewiesenes Geheimnis'), ('\\b[0-9a-f]{48,}\\b', 'langer Hexwert (HMAC/Schluessel-Verdacht)')]
CORE = Path(__file__).resolve().parents[1]
DOCUMENTS = tuple(CORE / name for name in (
    "README.md", "PUBLIC_TEST_SCOPE.md", "ARCHITECTURE.md",
    "CONTRIBUTING.md", "SECURITY.md", "LICENSE"))


def check_public_documents():
    missing = [str(p.relative_to(CORE)) for p in DOCUMENTS if not p.is_file()]
    require_equal(missing, [], f"public document set is incomplete: {missing}")
    findings = []
    for path in DOCUMENTS:
        text = path.read_text(encoding="utf-8")
        for pattern, category in SECRETS:
            for match in re.finditer(pattern, text):
                line = text[:match.start()].count("\n") + 1
                findings.append(f"{path.relative_to(CORE)}:{line} {category}")
    require_equal(findings, [], f"possible secret shapes in public documents: {findings}")
