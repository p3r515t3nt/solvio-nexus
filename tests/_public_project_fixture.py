"""Synthetic project documents for bundle safety; no owner plans or evidence."""
from contextlib import contextmanager
from pathlib import Path
import tempfile


@contextmanager
def project_fixture():
    with tempfile.TemporaryDirectory(prefix="solvio-public-knowledge-") as folder:
        root = Path(folder)
        documents = {
            "PROJECT.md": "# Synthetic project\n\n" + ("Synthetic bounded project document for a regression case.\n" * 400),
            "ROADMAP.md": "# Synthetic roadmap\n\nNo live claims are made by this fixture.\n",
            "docs/INDEX.md": "# Synthetic index\n\nFixture documents only.\n",
            "docs/project_state.yaml": "schema_version: 1\ndescribes_commit: " + "a" * 40 + "\n",
            "docs/agents/PROJECT_KNOWLEDGE_CONTRACT.md": "# Synthetic knowledge contract\n\nFixture only.\n",
            "docs/architecture/TRUST_BOUNDARY.md": "# Synthetic trust boundary\n\n" + "Long synthetic catalogue body that must not be copied. " * 8 + "\n",
        }
        # Catalogue titles make the capped head exceed 5,000 characters;
        # this exercises actual bundle truncation as well as debt truncation.
        for catalogue in ("decisions", "releases", "runbooks"):
            for number in range(1, 13):
                documents[f"docs/{catalogue}/synthetic-{number:02d}.md"] = (
                    "# Synthetic catalogue entry " + "x" * 140
                    + "\n\nSynthetic fixture only; no captured documents.\n"
                )
        debt = "# Synthetic debt register\n\n"
        for number in range(1, 241):
            heading = "##" if number % 2 else "###"
            separator = " — " if number % 2 else " · "
            debt += f"{heading} DEBT-{number:04d}{separator}Synthetic entry {number}\n\n**Einstufung:** normal\n\nSynthetic fixture explanation.\n\n"
        documents["docs/debt/TECH_DEBT.md"] = debt
        for relative, text in documents.items():
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        yield str(root)
