"""Local API access is not local inference (#294).

The public AI guide claimed nothing leaves the machine while recommending
cloud-backed CLIs. The company file and the API stay local; the agent's
model usually does not, and what it reads goes to its provider. The site
says so now; the README and the docs say the same, and these pins keep
them saying it.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_the_readme_separates_local_api_from_local_inference():
    md = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "What stays on your machine is the company file and the API." in md
    assert "A cloud-backed\nCLI's model does not" in md
    assert (
        "leaving your machine" not in md
        and "stay on your machine" not in md.replace("What stays on your machine", "")
    )


def test_the_docs_say_what_leaves_and_what_does_not():
    md = (ROOT / "docs" / "features.md").read_text(encoding="utf-8")
    assert "**What stays local, and what does not** (#294)." in md
    assert "a cloud-backed CLI's model is not" in md
    assert "running the CLI process locally is not enough by itself" in md
