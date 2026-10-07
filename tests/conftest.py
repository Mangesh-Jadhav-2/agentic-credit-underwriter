import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture(autouse=True)
def _isolated_audit_log(tmp_path, monkeypatch):
    """Tests must never append to the project's real audit log."""
    monkeypatch.setenv("AUDIT_LOG_DIR", str(tmp_path / "logs"))
