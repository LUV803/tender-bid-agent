"""Shortcut entry point: ``python -m bidding.parsing <pdf>``."""

from __future__ import annotations

import argparse
import sys

from ..console import configure_stdio
from .errors import ParseError
from .pipeline import parse_document


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = argparse.ArgumentParser(
        prog="python -m bidding.parsing",
        description="解析招标 PDF，产出 content.md、manifest.json 与图片",
    )
    parser.add_argument("pdf", help="PDF 文件路径")
    parser.add_argument(
        "--parser",
        choices=("auto", "cloud", "local"),
        default="auto",
        help="auto 优先 MinerU、失败回退 PyMuPDF；cloud 强制云端；local 仅本地",
    )
    parser.add_argument(
        "--strict-parser",
        action="store_true",
        help="云端解析不可用或失败时直接报错，不回退",
    )
    parser.add_argument("--force", action="store_true", help="忽略缓存，强制重解析")
    args = parser.parse_args(argv)

    try:
        manifest = parse_document(
            args.pdf,
            parser=args.parser,
            strict_parser=args.strict_parser,
            force=args.force,
        )
    except (ParseError, ValueError) as exc:
        print(f"解析失败：{exc}", file=sys.stderr)
        return 1

    print(f"doc_id        : {manifest['doc_id']}")
    print(f"parser        : {manifest['parser']}")
    print(f"doc_type      : {manifest['doc_type']}")
    print(f"pages         : {manifest['pages']}")
    print(f"sections      : {len(manifest['sections'])}")
    print(f"images        : {len(manifest['images'])}")
    print(f"markdown_path : {manifest['markdown_path']}")
    for warning in manifest.get("warnings", []):
        print(f"warning       : {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
