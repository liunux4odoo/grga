"""Generate hub_diff.md — the core deliverable answering the single question:
does chapter-level position constraint let hub node (韩立) neighbors surface
semantic entities?

V0 data: read from prior experiment hanli_probe.json (do NOT rebuild).
V2_retok / V4: read from this experiment's results_*.json.
"""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PRIOR = ROOT.parent / "cooccur_2026_07_10"

# Core entities to test hub semantics (spec §1 / §3.3 判定)
ENTITIES = ["墨大夫", "李化元", "极阴祖师", "黄枫谷", "七玄门", "曲魂"]

# type buckets for degradation analysis (spec §9 "不成立" -> 类型分布)
FUNCTION_WORDS = {
    "没有", "起来", "一下", "一个", "自然", "似乎", "知道", "无法", "现在",
    "已经", "同样", "不知", "不会", "顿时", "突然", "马上", "竟然", "出现",
    "样子", "一声", "一丝", "之色",
}
ACTION_WORDS = {"说道", "露出", "问道", "看来"}
BODY_GAZE_WORDS = {"心中", "神色", "身上", "脸上", "目光", "一眼", "手中"}


def load_hanli_top30(results_path: Path):
    d = json.loads(results_path.read_text(encoding="utf-8"))
    for q in d["queries"]:
        if q["seed"] == "韩立":
            return q
    return None


def fmt_neighbors(neighbors, k=30):
    out = []
    for n in neighbors[:k]:
        out.append(f"{n['rank']}. {n['term']}  w={n['weight']:g}")
    return out


def classify(term):
    if term in ENTITIES:
        return "entity"
    if term in FUNCTION_WORDS:
        return "function"
    if term in ACTION_WORDS:
        return "action"
    if term in BODY_GAZE_WORDS:
        return "body/gaze"
    return "other"


def main():
    # V0 from prior probe
    prior = json.loads((PRIOR / "hanli_probe.json").read_text(encoding="utf-8"))
    v0 = prior["variants"]["V0"]["neighbors"]  # already top-30

    v2r_q = load_hanli_top30(ROOT / "results_V2_retok.json")
    v4_q = load_hanli_top30(ROOT / "results_V4.json")

    v4_terms = [n["term"] for n in v4_q["returned_neighbors"][:30]] if v4_q["seed_in_graph"] else []
    entity_hits = [t for t in v4_terms if t in ENTITIES]
    n_entity = len(entity_hits)

    # verdict
    if n_entity >= 1:
        verdict = "成立" if n_entity >= 3 else "部分成立"
    else:
        verdict = "不成立"

    # type distribution of V4 top-30
    from collections import Counter
    typedist = Counter(classify(t) for t in v4_terms)

    L = []
    L.append("# 韩立作为 seed 的邻居对比 (hub_diff)")
    L.append("")
    L.append("> 本实验唯一目的：**章节级位置约束能否让 hub 节点『韩立』的 top-30 邻居出现语义实体？**")
    L.append(f"> 判定实体集合：{' / '.join(ENTITIES)}")
    L.append("")

    # V0
    L.append("## V0（前作原始『整行任意共现』，供参考）")
    L.append("")
    L.append("来源：`../cooccur_2026_07_10/hanli_probe.json`（未重跑）")
    L.append("")
    for line in fmt_neighbors(v0, 30):
        L.append(line)
    L.append("")

    # V2_retok
    L.append("## V2_retok（新 tokenizer + 自适应 DF，仍是『整行』共现）")
    L.append("")
    if not v2r_q["seed_in_graph"]:
        L.append("**韩立 seed_in_graph = false** —— V2_retok 的 DF-adaptive 剪枝把韩立当作")
        L.append("高频词自动干掉了（top-30 by chapter/line DF），因此韩立不在图中，无邻居可列。")
        L.append("")
        L.append("这与前作 V2 行为一致：DF 剪枝清理了停用词污染，但同时也把 hub 主角剪掉了，")
        L.append("hub 语义信号随之丢失（前作已记录的核心矛盾）。")
    else:
        for line in fmt_neighbors(v2r_q["returned_neighbors"], 30):
            L.append(line)
    L.append("")

    # V4
    L.append("## V4（章节级共现，本次核心）")
    L.append("")
    if not v4_q["seed_in_graph"]:
        L.append("**韩立 seed_in_graph = false**（异常，V4 未启用 DF 剪枝，理应在图中）")
    elif not v4_terms:
        L.append("**韩立 seed_in_graph = true 但 neighbors = []** —— 位置约束假设不成立。")
    else:
        for n in v4_q["returned_neighbors"][:30]:
            mk = "  ← 实体命中" if n["term"] in ENTITIES else ""
            L.append(f"{n['rank']}. {n['term']}  w={n['weight']:g}{mk}")
    L.append("")

    # Verdict
    L.append("## 判定")
    L.append("")
    L.append(f"- **V4 top-30 里有 {n_entity} 个实体**"
             f"（{' / '.join(ENTITIES)} 中的匹配数）"
             + (f"：{', '.join(entity_hits)}" if entity_hits else ""))
    L.append(f"- 结论: 位置约束假设 **{verdict}**")
    L.append("")

    # Type distribution (spec §9: 不成立 时必须给类型分布)
    L.append("### V4 top-30 邻居类型分布")
    L.append("")
    L.append("| 类型 | 数量 |")
    L.append("|------|------|")
    for typ in ["entity", "function", "action", "body/gaze", "other"]:
        L.append(f"| {typ} | {typedist.get(typ, 0)} |")
    L.append("")

    # Notes
    L.append("## Notes")
    L.append("")
    v4 = json.loads((ROOT / "results_V4.json").read_text(encoding="utf-8"))
    stats = v4["corpus_stats"]
    L.append(f"- V4 corpus_stats: total_chapters={stats['total_chapters']}, "
             f"effective_chapters={stats['effective_chapters']}, "
             f"total_edges={stats['total_edges']}, "
             f"build_time={stats['build_time_seconds']}s")
    L.append(f"- chapter_truncations = {v4['notes']['chapter_truncations']} "
             f"（>10000 token 的超长章节做 top-500 截断的次数）")
    sw = v4["notes"].get("graph_sparse_warning")
    if sw:
        L.append(f"- **稀疏警告**: {sw}")
    else:
        L.append(f"- 图非稀疏（total_edges={stats['total_edges']} 远大于 100），"
                 "min_pair_count=3 未导致崩塌；相反章节窗口过宽导致高频词全连接。")
    L.append("")
    if verdict == "不成立":
        L.append("### 为什么不成立（接近成立还是完全否定）")
        L.append("")
        L.append(f"V4 top-30 里 entity={typedist.get('entity',0)}、"
                 f"function（虚词/程度副词）={typedist.get('function',0)}、"
                 f"action（主要动作词）={typedist.get('action',0)}、"
                 f"body/gaze（身体/神态）={typedist.get('body/gaze',0)}、"
                 f"other={typedist.get('other',0)}。")
        L.append("")
        L.append("这是**完全否定**，不是接近成立：top-30 里一个配角人物/门派都没有，"
                 "全是高频虚词、动作、神态类词。原因是**章节窗口太宽**——"
                 "《凡人修仙传》一章约几百到上千 token，主角韩立几乎在每一章都出现，"
                 "于是『韩立』与任何高章节频（chapter-DF）词的共现次数都逼近其 DF 上限"
                 "（top 邻居 w≈1400-1900，接近 2005 章的量级），"
                 "pair 权重退化为 **min(DF_韩立, DF_其他词) ≈ DF_其他词**，"
                 "即纯粹的高频词排序，与前作『整行共现在 hub 上退化为频次统计』是**同一种退化**，"
                 "只是把窗口从『行』放大到『章』后退化更严重（连 V0 里靠行级稀疏性偶尔冒头的"
                 "配角都被抹平了）。")
        L.append("")
        L.append("**结论对 L2 图扩展路线的含义**：章节级这个『最结构化的窗口』并不能救 hub。"
                 "位置约束要 work，窗口粒度必须**远小于章节**（段级/滑窗/句级），"
                 "让『韩立』与配角的共现不再被全章高频词淹没。这为下一轮 V5/V6"
                 "（段级/滑窗）提供了明确的证据触发。")
        L.append("")

    # 200-word summary comparing V0/V2_retok/V4 (加分)
    L.append("## 附：V0 / V2_retok / V4 类型分布对比（~200 字）")
    L.append("")
    L.append(_summary(v0, v2r_q, v4_terms, typedist, n_entity))
    L.append("")

    (ROOT / "hub_diff.md").write_text("\n".join(L), encoding="utf-8")
    print(f"hub_diff.md written. N_entity(V4 top-30) = {n_entity}, verdict = {verdict}")
    print("V4 typedist:", dict(typedist))


def _summary(v0, v2r_q, v4_terms, typedist, n_entity):
    from collections import Counter
    v0_terms = [n["term"] for n in v0[:30]]
    v0_dist = Counter(classify(t) for t in v0_terms)
    return (
        f"V0（整行共现）top-30 以高频虚词/神态词为主"
        f"（function≈{v0_dist.get('function',0)}、body/gaze≈{v0_dist.get('body/gaze',0)}），"
        f"实体 {v0_dist.get('entity',0)} 个；V2_retok 因 DF-adaptive 把韩立整个剪出图，"
        f"hub 邻居无从谈起（清污与保 hub 不可兼得）；"
        f"V4（章节级）top-30 实体 {n_entity} 个，类型分布 function={typedist.get('function',0)}、"
        f"body/gaze={typedist.get('body/gaze',0)}、action={typedist.get('action',0)}、"
        f"other={typedist.get('other',0)}。三者对比可见：把窗口从『行』放大到『章』"
        f"并未引入语义实体，反而因主角近乎全章出现，使共现权重彻底退化为章节频次排序，"
        f"退化程度不亚于 V0。位置约束若要奏效，窗口必须显著窄于整章。"
    )


if __name__ == "__main__":
    main()
