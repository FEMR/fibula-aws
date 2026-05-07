"""Backward-compatible wrapper for the schema sync check."""

from schema_sync import main


if __name__ == '__main__':
    raise SystemExit(main(["--json"]))
