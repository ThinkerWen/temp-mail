"""Create a private configuration file without overwriting existing keys."""

import argparse
import os
import secrets
import sys
from pathlib import Path

import yaml
from cryptography.fernet import Fernet


class _TemplateLoader(yaml.SafeLoader):
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise ValueError("Template keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def initialize_config(template: Path, output: Path) -> None:
    document = yaml.load(template.read_text(encoding="utf-8"), Loader=_TemplateLoader)
    if not isinstance(document, dict) or document.keys() - {"app", "worker", "providers"}:
        raise ValueError("Invalid template sections")
    if not isinstance(document.get("app"), dict) or not isinstance(document.get("providers"), dict):
        raise ValueError("Template requires app and providers mappings")
    if "worker" in document and not isinstance(document["worker"], dict):
        raise ValueError("Template worker section must be a mapping")
    document["app"]["api_token"] = secrets.token_urlsafe(32)
    document["app"]["encryption_key"] = Fernet.generate_key().decode()
    rendered = yaml.safe_dump(document, sort_keys=False, allow_unicode=True)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(rendered)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        output.unlink(missing_ok=True)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Generate API and encryption keys in a new private configuration file")
    parser.add_argument("--output", type=Path, default=Path("config.yaml"))
    parser.add_argument("--template", type=Path, default=Path("config.example.yaml"))
    args = parser.parse_args(argv)
    try:
        initialize_config(args.template, args.output)
    except FileExistsError:
        print("Configuration already exists; refusing to overwrite its keys.", file=sys.stderr)
        return 1
    except (OSError, ValueError, yaml.YAMLError):
        # YAML exceptions include source lines; filesystem errors can also reveal private values.
        print("Cannot initialize configuration; check the template and destination directory.", file=sys.stderr)
        return 1
    print(f"Created configuration: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
