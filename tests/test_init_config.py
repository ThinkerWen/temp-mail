import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet

from app.config import Settings
from scripts import init_config

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def template(tmp_path):
    path = tmp_path / "config.example.yaml"
    path.write_bytes((PROJECT_ROOT / "config.example.yaml").read_bytes())
    return path


def test_cli_defaults_create_private_loadable_config_and_preserve_template_options(template, tmp_path):
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "init_config.py")], cwd=tmp_path, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    output = tmp_path / "config.yaml"
    settings = Settings.from_yaml(output)
    assert len(settings.api_token) >= 32
    cipher = Fernet(settings.encryption_key.encode())
    assert cipher.decrypt(cipher.encrypt(b"persisted-upstream-token")) == b"persisted-upstream-token"
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert settings.api_token not in result.stdout + result.stderr
    assert settings.encryption_key not in result.stdout + result.stderr
    original, generated = yaml.safe_load(template.read_text()), yaml.safe_load(output.read_text())
    assert generated["providers"] == original["providers"]
    assert generated["worker"] == original["worker"]
    assert generated["app"]["db_path"] == original["app"]["db_path"]
    assert original["app"]["api_token"] == original["app"]["encryption_key"] == ""


def test_explicit_paths_generate_distinct_keys_without_changing_working_directory(template, tmp_path, capsys):
    outputs = [tmp_path / "first.yaml", tmp_path / "second.yaml"]
    for output in outputs:
        assert init_config.main(["--output", str(output), "--template", str(template)]) == 0
    first, second = [Settings.from_yaml(output) for output in outputs]
    assert first.api_token != second.api_token
    assert first.encryption_key != second.encryption_key
    captured = capsys.readouterr()
    for value in (first.api_token, second.api_token, first.encryption_key, second.encryption_key):
        assert value not in captured.out + captured.err


def test_existing_output_is_never_overwritten_or_rekeyed(template, tmp_path, capsys):
    output = tmp_path / "config.yaml"
    arguments = ["--output", str(output), "--template", str(template)]
    assert init_config.main(arguments) == 0
    original = output.read_bytes()
    original_stat = output.stat()
    assert init_config.main(arguments) == 1
    assert output.read_bytes() == original
    assert output.stat().st_ino == original_stat.st_ino
    assert output.stat().st_mtime_ns == original_stat.st_mtime_ns
    captured = capsys.readouterr()
    assert "already exists" in captured.err
    assert "refusing to overwrite" in captured.err


@pytest.mark.parametrize(
    "contents",
    [
        'app:\n  api_token: "private-secret-without-closing-quote\nproviders: {}\n',
        "app: []\nproviders: {}\n",
        "app: {}\n",
        "app: {}\nproviders: {}\nproviders: {}\n",
        "app: {}\nproviders: {}\nworker: []\n",
    ],
)
def test_invalid_template_leaves_no_output_and_does_not_print_source(contents, tmp_path, capsys):
    template, output = tmp_path / "broken.yaml", tmp_path / "config.yaml"
    template.write_text(contents)
    assert init_config.main(["--template", str(template), "--output", str(output)]) == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert "private-secret" not in captured.out + captured.err
    assert captured.out == ""


def test_missing_output_directory_is_reported_without_creating_directories(template, tmp_path, capsys):
    output = tmp_path / "missing" / "config.yaml"
    assert init_config.main(["--template", str(template), "--output", str(output)]) == 1
    assert not output.parent.exists()
    assert "destination directory" in capsys.readouterr().err


def test_failed_write_removes_only_new_incomplete_output(template, tmp_path, monkeypatch, capsys):
    output = tmp_path / "config.yaml"

    def fail_sync(descriptor):
        raise OSError("private-disk-error-detail")

    monkeypatch.setattr(init_config.os, "fsync", fail_sync)
    assert init_config.main(["--template", str(template), "--output", str(output)]) == 1
    assert not output.exists()
    captured = capsys.readouterr()
    assert "private-disk-error-detail" not in captured.out + captured.err


def test_existing_symlink_is_not_followed_or_replaced(template, tmp_path, capsys):
    target = tmp_path / "existing-config.yaml"
    target.write_text("original-secret-configuration")
    output = tmp_path / "config.yaml"
    output.symlink_to(target)
    assert init_config.main(["--template", str(template), "--output", str(output)]) == 1
    assert output.is_symlink()
    assert target.read_text() == "original-secret-configuration"
    assert "original-secret-configuration" not in capsys.readouterr().err
