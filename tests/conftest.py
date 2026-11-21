"""Skip, with a stated reason, the tests whose environment is not here. No assertion is changed or weakened: a test that runs runs
unmodified, and a test that needs something absent is reported as skipped, never as failed.

Markers (registered in pytest.ini):
  requires_es        needs a scratch Elasticsearch: LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW all set (tests/security/*, applied by path)
  requires_embedder  needs the pinned real embedder: LC_E5_DIR (tests that already `skipUnless` on it are left as they are)
  requires_model     needs a real model call; nothing in tests/ makes one, the marker is for tests that would
  requires_linux_fs  needs Linux filesystem features the product uses on the run host: renameat2(RENAME_EXCHANGE) for the journal
                     swap (living_context/gateway/fsx.py: "Linux only") and os.posix_fallocate (gateway/epoch_journal.py)

Tests of this kind are marked by `@pytest.mark.<name>` or, for existing unittest classes that must stay untouched, listed below.
"""
import os
import sys
from pathlib import Path

import pytest

HAVE_ES = all(os.environ.get(k) for k in ("LC_TEST_ES_URL", "LC_TEST_CA", "LC_TEST_PW"))
HAVE_EMBEDDER = bool(os.environ.get("LC_E5_DIR"))
HAVE_MODEL = os.environ.get("LC_ALLOW_MODEL_CALLS") == "1"      # no test here needs it; kept explicit so one never runs by accident
HAVE_LINUX_FS = sys.platform.startswith("linux") and hasattr(os, "posix_fallocate")

# unit tests that exercise the journal's atomic name exchange or its preallocation; they are expected to run on Linux (CI, the run host); not verified there from this checkout
LINUX_FS_TESTS = (
    "tests/unit/test_checked_answers.py::Provision::test_control_a_tenant_is_provisioned_and_registered",
    "tests/unit/test_checked_answers.py::Provision::test_a_registry_read_that_proves_no_absence_refuses",
    "tests/unit/test_checked_answers.py::Provision::test_a_registry_record_that_did_not_land_is_an_error",
    "tests/unit/test_checked_answers.py::PoolManifest::test_a_pool_whose_manifest_did_not_land_is_not_created",
    "tests/unit/test_journal_lock.py::JournalLock::test_a_second_process_does_not_start_while_the_first_holds_the_journal",
)

_ES_FILES: dict = {}


def _needs_es(path: Path) -> bool:
    """A file under tests/security that reads the scratch-cluster variables."""
    if path not in _ES_FILES:
        try:
            _ES_FILES[path] = "LC_TEST_ES_URL" in path.read_text()
        except OSError:
            _ES_FILES[path] = False
    return _ES_FILES[path]


def pytest_configure(config):
    for name, text in (("requires_es", "needs a scratch Elasticsearch (LC_TEST_ES_URL, LC_TEST_CA, LC_TEST_PW)"),
                       ("requires_embedder", "needs the pinned real embedder (LC_E5_DIR)"),
                       ("requires_model", "needs a real model call (LC_ALLOW_MODEL_CALLS=1)"),
                       ("requires_linux_fs", "needs renameat2(RENAME_EXCHANGE) and os.posix_fallocate (Linux)")):
        config.addinivalue_line("markers", "%s: %s" % (name, text))


def pytest_collection_modifyitems(config, items):
    root = Path(str(config.rootpath))
    for item in items:
        path = Path(str(item.path))
        rel = item.nodeid
        if "tests/security/" in path.as_posix() and _needs_es(path):
            item.add_marker(pytest.mark.requires_es)
        if any(rel == n or rel.startswith(n + "[") for n in LINUX_FS_TESTS):
            item.add_marker(pytest.mark.requires_linux_fs)
        for marker, have, why in (
                ("requires_es", HAVE_ES, "no scratch Elasticsearch: set LC_TEST_ES_URL, LC_TEST_CA and LC_TEST_PW"),
                ("requires_embedder", HAVE_EMBEDDER, "the pinned embedder is not available: set LC_E5_DIR"),
                ("requires_model", HAVE_MODEL, "a real model call is needed: set LC_ALLOW_MODEL_CALLS=1"),
                ("requires_linux_fs", HAVE_LINUX_FS, "needs Linux renameat2(RENAME_EXCHANGE) and os.posix_fallocate (%s)" % sys.platform)):
            if item.get_closest_marker(marker) and not have:
                item.add_marker(pytest.mark.skip(reason=why))
