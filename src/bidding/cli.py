"""Command line interface for parsing, extraction and indexing."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import Settings, get_settings
from .console import configure_stdio
from .extraction import ExtractError, run_extract
from .indexing import (
    IngestError,
    index_manifest_path,
    inspect_document,
    run_ingest,
)
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


def _cmd_extract(args: argparse.Namespace) -> int:
    def progress(message: str) -> None:
        print(message, flush=True)

    extraction = run_extract(
        args.doc_id,
        force=args.force,
        unit_limit=args.limit,
        progress=progress,
    )
    meta = extraction.get("extraction_meta") or {}
    print()
    print(f"project_name  : {extraction.get('project_name') or '-'}")
    print(f"tender_number : {extraction.get('tender_number') or '-'}")
    print(f"budget        : {extraction.get('budget_amount_text') or '-'}")
    print(f"deadline      : {extraction.get('bid_deadline') or '-'}")
    print(f"evaluation    : {extraction.get('evaluation_method') or '-'}")
    print(f"factors       : {len(extraction.get('evaluation_factors') or [])}")
    print(f"preliminary   : {len(extraction.get('preliminary_review') or [])}")
    print(f"qualifications: {len(extraction.get('qualifications') or [])}")
    print(f"bid_format    : {len((extraction.get('bid_format') or {}).get('items') or [])}")
    print(f"key_dates     : {len(extraction.get('key_dates') or [])}")
    print(f"ungrounded    : {len(extraction.get('ungrounded_fields') or [])}")
    print(f"units         : {meta.get('processed_units', 0)}/{meta.get('unit_count', 0)} "
          f"(cached={meta.get('cached_units', 0)}, failed={len(meta.get('failed_units') or [])})")
    print(f"partial       : {meta.get('partial', False)}")
    print(f"elapsed       : {meta.get('elapsed_seconds', 0)}s")
    for warning in meta.get("warnings") or []:
        print(f"warning       : {warning}")
    return 0


def _cmd_ingest(args: argparse.Namespace) -> int:
    def progress(message: str) -> None:
        print(message, flush=True)

    source = Path(args.source)
    if source.is_file() and source.suffix.lower() == ".pdf":
        manifest = parse_document(
            str(source),
            parser=args.parser,
            strict_parser=args.strict_parser,
            force=args.force,
        )
        doc_id = str(manifest["doc_id"])
        print(f"doc_id        : {doc_id}")
        _print_manifest(manifest)
        if not args.skip_extract:
            run_extract(doc_id, force=args.force, progress=progress)
    elif source.suffix.lower() == ".pdf":
        raise ValueError(f"找不到 PDF：{source}")
    else:
        doc_id = args.source

    index = run_ingest(
        doc_id,
        verify=not args.no_verify,
        verify_query=args.query,
        progress=progress,
    )
    print()
    print(f"doc_id        : {index['doc_id']}")
    print(f"collection    : {index['collection']}")
    print(f"chunks        : {index['chunks']} (stored={index['stored_chunks']})")
    print(f"embedding     : {index['embedding_model']} "
          f"({index['embedding_dimensions']}d, calls={index['embedding_calls']})")
    print(f"index_manifest: {index_manifest_path(doc_id)}")
    verification = index.get("verification") or {}
    if verification:
        print(f"verify        : {'OK' if verification.get('ok') else 'FAIL'} "
              f"query={verification.get('query', '')!r}")
        for hit in (verification.get("hits") or [])[:3]:
            print(f"  - {hit.get('section_title')} {hit.get('page_hint')} "
                  f"distance={hit.get('distance')}")
            print(f"    {hit.get('excerpt', '')[:160]}")
    for warning in index.get("warnings") or []:
        print(f"warning       : {warning}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    result = inspect_document(
        args.doc_id,
        query=args.query,
        top_k=args.top_k,
    )
    print(f"doc_id        : {result['doc_id']}")
    print(f"doc_type      : {result['doc_type']}")
    print(f"source        : {result['source_name']}")
    print(f"collection    : {result['collection']}")
    print(f"chunks        : {result['chunks']}")
    index_manifest = result.get("index_manifest") or {}
    if index_manifest:
        print(f"embedding     : {index_manifest.get('embedding_model')} "
              f"({index_manifest.get('embedding_dimensions')}d)")
        print(f"indexed_at    : {index_manifest.get('created_at')}")
    hits = result.get("hits") or []
    if args.query:
        print(f"query         : {args.query!r}")
    for position, hit in enumerate(hits, start=1):
        metadata = hit.get("metadata") or {}
        print(f"[{position}] distance={hit.get('distance')} "
              f"{metadata.get('section_path', '')} {metadata.get('page_hint', '')}")
        print(f"    {(hit.get('document') or '')[:220].replace(chr(10), ' ')}")
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
    print(f"llm          : {settings.llm_provider}/{settings.llm_model} "
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

    extract_parser = subparsers.add_parser("extract", help="结构化抽取已解析文档")
    extract_parser.add_argument("doc_id", help="解析后的文档 ID")
    extract_parser.add_argument("--force", action="store_true", help="忽略缓存重新抽取")
    extract_parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅处理前 N 个单元（调试用，结果标记为 partial）",
    )
    extract_parser.set_defaults(func=_cmd_extract)

    ingest_parser = subparsers.add_parser(
        "ingest", help="分块并向量化写入 Chroma（可给 PDF 或 doc_id）"
    )
    ingest_parser.add_argument("source", help="PDF 路径或已解析的 doc_id")
    ingest_parser.add_argument(
        "--parser",
        choices=("auto", "cloud", "local"),
        default="auto",
        help="给 PDF 时的解析器选择",
    )
    ingest_parser.add_argument(
        "--strict-parser",
        action="store_true",
        help="云端解析不可用时直接报错",
    )
    ingest_parser.add_argument(
        "--force",
        action="store_true",
        help="给 PDF 时强制重新解析和抽取",
    )
    ingest_parser.add_argument(
        "--skip-extract",
        action="store_true",
        help="给 PDF 时只解析和索引，不做结构化抽取",
    )
    ingest_parser.add_argument(
        "--no-verify",
        action="store_true",
        help="跳过入库后的检索自检",
    )
    ingest_parser.add_argument("--query", default=None, help="自检查询语句")
    ingest_parser.set_defaults(func=_cmd_ingest)

    inspect_parser = subparsers.add_parser(
        "inspect", help="查看 Chroma 分块并做检索测试"
    )
    inspect_parser.add_argument("doc_id", help="已入库的文档 ID")
    inspect_parser.add_argument("--query", default=None, help="检索测试查询")
    inspect_parser.add_argument("--top-k", type=int, default=5, help="返回条数")
    inspect_parser.set_defaults(func=_cmd_inspect)

    doctor_parser = subparsers.add_parser("doctor", help="检查配置与服务连通性")
    doctor_parser.set_defaults(func=_cmd_doctor)
    return parser


def main(argv: list[str] | None = None) -> int:
    configure_stdio()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except (ParseError, ExtractError, IngestError, ValueError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
