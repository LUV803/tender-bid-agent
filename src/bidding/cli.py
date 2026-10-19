"""Command line interface for parsing, extraction and indexing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings, get_settings
from .console import configure_stdio
from .parsing import ParseError, parse_document


def _print_manifest(manifest: dict) -> None:
    print(f"doc_id        : {manifest['doc_id']}")
    print(f"parser        : {manifest['parser']}")
    print(f"doc_type      : {manifest['doc_type']}")
    print(f"pages         : {manifest['pages']}")
    print(f"sections      : {len(manifest['sections'])}")
    print(f"images        : {len(manifest['images'])}")
    print(f"markdown_path : {manifest['markdown_path']}")
    for warning in manifest.get("warnings", []):
        print(f"warning       : {warning}")


def _cmd_parse(args: argparse.Namespace) -> int:
    manifest = parse_document(
        args.pdf,
        parser=args.parser,
        strict_parser=args.strict_parser,
        force=args.force,
    )
    _print_manifest(manifest)
    return 0


def _service_status(settings: Settings) -> list[tuple[str, bool, str]]:
    checks: list[tuple[str, bool, str]] = []
    try:
        import pymupdf

        checks.append(("PyMuPDF", True, pymupdf.__version__))
    except Exception as exc:
        checks.append(("PyMuPDF", False, str(exc)))

    try:
        import chromadb

        client = chromadb.HttpClient(
            host=settings.chroma_host, port=settings.chroma_port
        )
        heartbeat = client.heartbeat()
        checks.append(
            ("Chroma", True, f"{settings.chroma_host}:{settings.chroma_port} (heartbeat={heartbeat})")
        )
    except Exception as exc:
        checks.append(
            ("Chroma", False, f"{settings.chroma_host}:{settings.chroma_port} ({exc})")
        )

    try:
        import redis

        redis.Redis.from_url(
            settings.redis_url, socket_connect_timeout=2, socket_timeout=2
        ).ping()
        checks.append(("Redis", True, settings.redis_url))
    except Exception as exc:
        checks.append(("Redis", False, f"{settings.redis_url} ({exc})"))

    return checks


def _cmd_doctor(args: argparse.Namespace) -> int:
    settings = get_settings()
    settings.ensure_directories()
    print(f"project_root : {settings.project_root}")
    print(f"upload_dir   : {settings.upload_dir}")
    print(f"llm          : {settings.llm_provider}/{settings.deepseek_model} "
          f"key={'yes' if settings.llm_configured else 'no'}")
    print(f"embedding    : {settings.embedding_model} "
          f"key={'yes' if settings.embedding_configured else 'no'} "
          f"dimensions={settings.embedding_dimensions}")
    print(f"mineru       : mode={settings.mineru_mode} "
          f"key={'yes' if settings.mineru_configured else 'no'}")
    print(f"chroma       : {settings.chroma_host}:{settings.chroma_port} "
          f"dir={settings.chroma_dir}")
    print(f"redis        : {settings.redis_url}")
    for name, ok, detail in _service_status(settings):
        print(f"{name:<12} : {'OK' if ok else 'FAIL'} {detail}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bidding",
        description="招标文件解析、结构化抽取与检索索引工具",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    parse_parser = subparsers.add_parser("parse", help="解析 PDF")
    parse_parser.add_argument("pdf", help="PDF 文件路径")
    parse_parser.add_argument(
        "--parser",
        choices=("auto", "cloud", "local"),
        default="auto",
        help="auto 优先 MinerU、失败回退 PyMuPDF；cloud 强制云端；local 仅本地",
    )
    parse_parser.add_argument(
        "--strict-parser",
        action="store_true",
        help="云端不可用或失败时直接报错，不回退",
    )
    parse_parser.add_argument("--force", action="store_true", help="强制重解析")
    parse_parser.set_defaults(func=_cmd_parse)

    doctor_parser = subparsers.add_parser("doctor", help="检查配置与服务连通性")
    doctor_parser.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ParseError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
