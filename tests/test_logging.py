import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LOG_PREFIX = re.compile(r"^\d{4}-\d{2}-\d{2} .* \| (?:INFO|WARNING|ERROR|Level 35)\s* \| PID \d+ \| ")


def run_logging_script(tmp_path, source):
    script = tmp_path / "logging_probe.py"
    script.write_text(textwrap.dedent(source))
    environment = dict(os.environ, PYTHONPATH=str(PROJECT_ROOT))
    return subprocess.run([sys.executable, str(script)], cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=10)


def test_configure_logging_unifies_existing_loggers_and_is_idempotent(tmp_path):
    result = run_logging_script(
        tmp_path,
        """
        import logging
        import sys

        from loguru import logger
        from app.logging import configure_logging

        dependency = logging.getLogger("dependency.client")
        dependency.addHandler(logging.StreamHandler(sys.stderr))
        dependency.propagate = False
        dependency.setLevel(logging.ERROR)
        dependency.disabled = True
        configure_logging()
        configure_logging()
        logger.info("native value={}", 42)
        dependency.warning("dependency value=%s", 43)
        dependency.log(35, "custom severity")
        logging.getLogger("late.dependency").info("late logger")
        logger.debug("filtered debug")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    lines = result.stderr.splitlines()
    assert len(lines) == 4, result.stderr
    assert all(LOG_PREFIX.match(line) for line in lines), result.stderr
    assert sum("native value=42" in line for line in lines) == 1
    assert sum("dependency value=43" in line for line in lines) == 1
    assert "dependency.client:" in lines[1]
    assert "WARNING" in lines[1]
    assert "Level 35" in lines[2]
    assert "late.dependency:" in lines[3]


def test_stdlib_exception_keeps_traceback_without_local_values(tmp_path):
    result = run_logging_script(
        tmp_path,
        """
        import logging

        from app.logging import configure_logging

        configure_logging()
        sensitive_value = "private-value-that-must-not-appear"
        try:
            int(sensitive_value[:0])
        except ValueError:
            logging.getLogger("dependency.failure").exception("request failed")
        """,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr.count("request failed") == 1
    assert "dependency.failure:" in result.stderr
    assert "Traceback (most recent call last):" in result.stderr
    assert "ValueError:" in result.stderr
    assert "private-value-that-must-not-appear" not in result.stderr


def test_uvicorn_dict_config_and_warnings_share_loguru_sink(tmp_path):
    result = run_logging_script(
        tmp_path,
        """
        import logging
        import logging.config
        import warnings

        from app.logging import UVICORN_LOG_CONFIG, configure_logging

        logging.config.dictConfig(UVICORN_LOG_CONFIG)
        logging.getLogger("uvicorn").info("parent ready")
        logging.getLogger("uvicorn.error").info("server ready")
        logging.getLogger("uvicorn.access").info('%s - "%s %s HTTP/%s" %d', "127.0.0.1:1234", "GET", "/probe", "1.1", 200)
        configure_logging()
        warnings.warn("captured-warning", UserWarning)
        """,
    )
    assert result.returncode == 0, result.stderr
    for name, message in [("uvicorn", "parent ready"), ("uvicorn.error", "server ready"), ("uvicorn.access", "GET /probe HTTP/1.1")]:
        matches = [line for line in result.stderr.splitlines() if message in line]
        assert len(matches) == 1, result.stderr
        assert LOG_PREFIX.match(matches[0]), result.stderr
        assert f"{name}:" in matches[0]
    warning_line = next(line for line in result.stderr.splitlines() if "py.warnings:" in line)
    assert LOG_PREFIX.match(warning_line), result.stderr
    assert "captured-warning" in warning_line
