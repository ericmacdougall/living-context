"""Repository hygiene (BUILD-PLAN Step 1).

Scans tracked files for absolute home paths, private-key headers, strings
shaped like Elasticsearch API keys and the dataset's gold-id prefix. The
patterns are assembled from pieces so that this file does not match itself.
"""
import base64
import binascii
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

HOME_PATH = re.compile(r"(?:/Us" r"ers/|/ho" r"me/)[A-Za-z0-9._-]+|[A-Za-z]:\\Us" r"ers\\")
KEY_HEADER = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIV" r"ATE KEY-----")
API_KEY_PREFIX = re.compile(r"ApiK" r"ey\s+[A-Za-z0-9+/_=-]{30,}")
B64_TOKEN = re.compile(r"[A-Za-z0-9+/_-]{40,100}={0,2}")
ES_KEY_DECODED = re.compile(r"^[A-Za-z0-9_-]{20}:[A-Za-z0-9_-]{20,24}$")
GOLD_ID = re.compile(r"\banswer" r"_[0-9a-f]{6,}(?:_\d+)?\b")


def es_key_shaped(token: str) -> bool:
    """True when the token decodes to `<20 char id>:<20 to 24 char secret>`."""
    pad = "=" * (-len(token.rstrip("=")) % 4)
    for alt in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            text = alt(token.rstrip("=") + pad).decode("ascii")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            continue
        if ES_KEY_DECODED.match(text):
            return True
    return False


def findings(text: str) -> list[str]:
    out = []
    if HOME_PATH.search(text):
        out.append("home path")
    if KEY_HEADER.search(text):
        out.append("private key header")
    if API_KEY_PREFIX.search(text):
        out.append("api key header")
    if any(es_key_shaped(t) for t in B64_TOKEN.findall(text)):
        out.append("api key shaped string")
    if GOLD_ID.search(text):
        out.append("gold id prefix")
    return out


def tracked_files(root: Path) -> list[Path]:
    """Tracked files; if the tree is not a git checkout, every file under root."""
    try:
        names = subprocess.run(["git", "-C", str(root), "ls-files", "-z", "--cached", "--others",
                                "--exclude-standard"], capture_output=True, check=True).stdout
        files = [root / n for n in names.decode().split("\0") if n]
    except (OSError, subprocess.CalledProcessError):
        files = [p for p in root.rglob("*") if p.is_file() and ".git" not in p.parts
                 and "__pycache__" not in p.parts and ".venv" not in p.parts]
    return [p for p in files if p.is_file()]


def scan(root: Path) -> dict[str, list[str]]:
    result = {}
    for p in tracked_files(root):
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        f = findings(text)
        if f:
            result[str(p.relative_to(root))] = f
    return result


class RepoHygiene(unittest.TestCase):
    def test_repository_is_clean(self):
        self.assertEqual(scan(ROOT), {})

    def test_planted_file_turns_the_scan_red_and_removal_turns_it_green(self):
        fake_key = base64.b64encode(("A" * 20 + ":" + "B" * 22).encode()).decode()
        planted = "\n".join([
            "key = " + fake_key,
            "path = " + "/Us" + "ers/someone/work",
            "answer" + "_4be1b6b4_2",
            "-----BEGIN " + "PRIVATE KEY-----",
        ])
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "clean.txt").write_text("nothing here\n")
            self.assertEqual(scan(root), {})
            (root / "planted.txt").write_text(planted)
            got = scan(root)
            self.assertEqual(sorted(got["planted.txt"]),
                             ["api key shaped string", "gold id prefix", "home path", "private key header"])
            (root / "planted.txt").unlink()
            self.assertEqual(scan(root), {})

    def test_each_pattern_fires_alone_and_benign_text_does_not(self):
        self.assertEqual(findings("answer_session_ids and answer_judge are field names"), [])
        self.assertEqual(findings("/path/to/ca.pem and 127.0.0.1"), [])
        self.assertEqual(findings("a" * 60), [])
        self.assertEqual(findings("/ho" + "me/x/y"), ["home path"])
        self.assertEqual(findings("ApiK" + "ey " + "Z" * 40), ["api key header"])

    def test_config_example_loads(self):
        from living_context import config
        s = config.load(str(ROOT / "config.example.toml"))
        self.assertEqual(s.constants.f_max, 24)


if __name__ == "__main__":
    unittest.main()
