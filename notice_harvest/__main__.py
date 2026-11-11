"""命令行入口：

    python -m notice_harvest scan \
        --site http://127.0.0.1:8090 \
        --state-dir ./state \
        --round R1
"""
from __future__ import annotations

import argparse
import json
import sys

from .collector import HarvestError, collect_round


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="notice_harvest",
        description="消防科普公告的轮次式增量采集",
    )
    subparsers = parser.add_subparsers(dest="command")

    scan = subparsers.add_parser("scan", help="执行一轮采集")
    scan.add_argument("--site", required=True, help="来源站根地址，例如 http://127.0.0.1:8090")
    scan.add_argument("--state-dir", required=True, help="采集状态与证据目录")
    scan.add_argument("--round", required=True, help="轮次标识；重复执行同一标识返回相同结果")
    scan.add_argument("--timeout", type=float, default=10.0, help="单次 HTTP 超时秒数")
    scan.add_argument("--retries", type=int, default=0, help="网络异常时的额外重试次数")
    scan.add_argument(
        "--max-rescans",
        type=int,
        default=2,
        help="列表变化后的最多补扫次数（至少 1，默认 2，即最多读取 3 遍列表）",
    )
    scan.add_argument("--max-pages", type=int, default=100, help="单遍列表最多页数")
    scan.add_argument(
        "--require-complete",
        action="store_true",
        help="本轮不完整时以退出码 1 结束（默认仍返回 0，由结果中的 complete 字段判断）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command != "scan":
        parser.print_help()
        return 2

    try:
        result = collect_round(
            site=args.site,
            state_dir=args.state_dir,
            round_id=args.round,
            timeout=args.timeout,
            retries=args.retries,
            max_rescans=args.max_rescans,
            max_pages=args.max_pages,
        )
    except (HarvestError, ValueError, OSError) as exc:
        print(f"采集失败：{exc}", file=sys.stderr)
        return 2

    json.dump(result, sys.stdout, ensure_ascii=False, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    if args.require_complete and not result.get("complete"):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
