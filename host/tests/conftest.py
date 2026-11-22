import pytest


@pytest.fixture(autouse=True)
def _data_dir(tmp_path, monkeypatch):
    """Keep the daemon's history and UI token out of the real data folder."""
    monkeypatch.setenv("KOPPAR_DATA", str(tmp_path / "data"))
