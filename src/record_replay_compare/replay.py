from __future__ import annotations


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description="Record and replay HTTP requests for comparison.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "base",
        type=str,
        help="Base URL for the base recorder.",
    )

    args = parser.parse_args(argv)

    return args.func(args) if hasattr(args, "func") else 0
