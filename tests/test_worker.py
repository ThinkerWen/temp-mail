import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app import worker


@pytest.mark.parametrize("once", [False, True])
def test_worker_retries_storage_error_only_in_continuous_mode(monkeypatch, once):
    settings = SimpleNamespace(worker_poll_seconds=1)
    run_once = Mock(side_effect=[sqlite3.OperationalError("temporary lock"), {"operations": 1, "synced": 0, "expired": 0}])
    service = SimpleNamespace(settings=settings, run_once=run_once)
    pause = Mock(side_effect=[None, KeyboardInterrupt])
    monkeypatch.setattr(worker.Settings, "from_yaml", lambda: settings)
    monkeypatch.setattr(worker, "Service", lambda config: service)
    monkeypatch.setattr(worker.time, "sleep", pause)
    monkeypatch.setattr("sys.argv", ["worker", "--once"] if once else ["worker"])
    if once:
        with pytest.raises(sqlite3.OperationalError, match="temporary lock"):
            worker.main()
        assert run_once.call_count == 1
        pause.assert_not_called()
    else:
        worker.main()
        assert run_once.call_count == 2
        assert pause.call_count == 2
