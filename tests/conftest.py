import pytest


@pytest.fixture(autouse=True)
def isolate_observation_journal(tmp_path, monkeypatch):
    monkeypatch.setenv('BREAKOUT_OUTBOX_PATH', str(tmp_path / 'outbox.json'))
    monkeypatch.setenv('BREAKOUT_TRACK_WRITER', 'auto-trade')
