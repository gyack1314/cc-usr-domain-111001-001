"""端到端演示：从干净目录完成一次扫描，再依次演示

1. 修订（正文变化、URL 改写但来源编号不变）
2. 半途失败（取页 503，轮次不完整、不撤下）
3. 两次完整缺席后撤下
4. 重新出现后恢复

并额外演示翻页中插入公告（列表变化补扫）与重复游标。脚本只使用标准库，
并通过真实 HTTP 访问本地来源站。
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from notice_harvest import collect_round  # noqa: E402
from source_site.__main__ import running  # noqa: E402


def set_phase(base_url: str, phase: str) -> None:
    request = urllib.request.Request(
        base_url + "/__control",
        data=json.dumps({"phase": phase}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=5) as response:
        response.read()


def summarize(title: str, result: dict) -> None:
    changes = result["changes"]
    print("=" * 72)
    print(title)
    print(f"  轮次: {result['round_id']}  完整: {result['complete']}  序号: {result['seq']}")
    if result["issues"]:
        print("  问题: " + ", ".join(sorted({issue["type"] for issue in result["issues"]})))
    print(
        "  新增: "
        + _ids(changes["new"], restored=True)
        + " | 修订: "
        + _ids(changes["revised"])
        + " | 撤下: "
        + _ids(changes["withdrawn"])
        + " | 无法确认: "
        + _ids(changes["unconfirmed"])
    )
    if result["absent_pending"]:
        pending = ", ".join(
            f"{item['source_id']}({item['consecutive_absent_complete_scans']}/2)"
            for item in result["absent_pending"]
        )
        print("  待撤下（缺席计数未满 2）: " + pending)
    print(
        f"  详情: fetched={result['detail_stats']['fetched']} "
        f"not_modified={result['detail_stats']['not_modified']} "
        f"failed={result['detail_stats']['failed']}"
    )
    print("  证据目录: " + result["evidence"]["site_state_dir"])


def _ids(items: list[dict], restored: bool = False) -> str:
    if not items:
        return "-"
    parts = []
    for item in items:
        label = item["source_id"]
        if restored and item.get("restored"):
            label += "(恢复)"
        if "changed_fields" in item:
            label += f"[{','.join(item['changed_fields'])}]"
        parts.append(label)
    return ", ".join(parts)


def main() -> int:
    parser = argparse.ArgumentParser(description="公告采集端到端演示")
    parser.add_argument(
        "--state-dir",
        default=None,
        help="状态目录；缺省时在系统临时目录下新建一个干净目录",
    )
    args = parser.parse_args()

    if args.state_dir:
        state_dir = args.state_dir
        Path(state_dir).mkdir(parents=True, exist_ok=True)
    else:
        state_dir = tempfile.mkdtemp(prefix="notice-harvest-demo-")
    print(f"状态目录: {state_dir}")

    with running() as (base_url, _source):
        set_phase(base_url, "base")
        r1 = collect_round(site=base_url, state_dir=state_dir, round_id="R1-初始扫描")
        summarize("1) 干净目录的首次完整扫描（n1 置顶跨页，仍只算一篇）", r1)

        set_phase(base_url, "revised")
        r2 = collect_round(site=base_url, state_dir=state_dir, round_id="R2-修订")
        summarize("2) 修订：n1 正文改变且 URL 改写为 /detail/n1?print=1", r2)

        set_phase(base_url, "partial")
        r3 = collect_round(site=base_url, state_dir=state_dir, round_id="R3-半途失败")
        summarize("3) 半途失败：第 2 页 503，n3 无法确认但不会被撤下", r3)

        set_phase(base_url, "missing")
        r4 = collect_round(site=base_url, state_dir=state_dir, round_id="R4-缺席第一次")
        r5 = collect_round(site=base_url, state_dir=state_dir, round_id="R5-缺席第二次")
        summarize("4a) 完整扫描缺席第 1 次（计数 1/2，不撤下）", r4)
        summarize("4b) 完整扫描缺席第 2 次（连续两次完整缺席，撤下 n1）", r5)

        set_phase(base_url, "base")
        r6 = collect_round(site=base_url, state_dir=state_dir, round_id="R6-重新出现")
        summarize("5) 重新出现：n1 恢复为有效公告", r6)

        set_phase(base_url, "moving")
        r7 = collect_round(site=base_url, state_dir=state_dir, round_id="R7-翻页中插入")
        summarize("6) 翻页中插入 n4：补扫合并证据，轮次标记为不完整", r7)

        set_phase(base_url, "loop")
        r8 = collect_round(site=base_url, state_dir=state_dir, round_id="R8-重复游标")
        summarize("7) 重复游标：立即停止翻页并标记不完整", r8)

        print("=" * 72)
        print("同一轮次重复执行返回相同结果（不发新 HTTP 请求）:")
        again = collect_round(
            site=base_url, state_dir=state_dir, round_id="R1-初始扫描"
        )
        print(f"  R1 幂等一致: {again == r1}")
        print(f"演示结束，状态目录保留供检查: {state_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
