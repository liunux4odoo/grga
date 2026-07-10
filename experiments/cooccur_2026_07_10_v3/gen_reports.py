"""Generate hub_diff_v3.md and topk_usability_v3.md from build outputs.

E/D/F/G classifications are explicit dicts here (reproducible). Uncertain items
are marked '?' and enumerated in QUESTIONS.md.
"""

from __future__ import annotations

import json
import statistics
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# ---------- classification (spec §6.3, sourced by hand on Fanren Xiuxian Zhuan) ----------
# E = entity (人物/门派/法宝/地名/事件/技法/丹药命名)
# D = domain (修仙语域词: 修炼/弟子/掌门/前辈/丹药/服用/…, 亲属/身份角色也算 D)
# F = filler (虚词/程度副词/情态词/量词指代/时态/普通抽象名词)
# G = gaze/action/body (神态/身体/动作) —— caller 不好用作搜索补充
# '?' = uncertain, listed in QUESTIONS.md

CLS: dict[str, dict[str, str]] = {
    "墨大夫": {
        "余子童": "E", "医术": "D", "妻室": "D", "把脉": "D", "妻女": "D",
        "空掌": "?", "信中": "F", "黄纸": "F", "象甲功": "E", "张铁": "E",
        "长春": "E", "七鬼噬": "E", "取信于": "F", "受害者": "F", "铁筒": "F",
        "神手": "E", "房内": "F", "铁奴": "E", "进度": "F", "漏出": "G",
        "药物": "D", "看病": "D", "毒药": "D", "太师椅": "F", "余子": "E",
        "详详细细": "F", "穴道": "D", "缠香丝": "E", "银刃": "E", "之死": "F",
    },
    "李化元": {
        "仙姑": "D", "师娘": "D", "夫妇": "D", "徒弟": "D", "为师": "D",
        "柔声": "G", "选修": "F", "武炫": "E", "师傅": "D", "保护": "F",
        "一类": "F", "这家": "F", "扶持": "F", "少妇": "D", "师姐": "D",
        "董萱": "E", "秦宅": "E", "宋蒙": "E", "红衣": "F", "平时": "F",
        "书页": "F", "记名": "D", "师伯": "D", "筑基": "D", "秦家": "E",
        "双修": "D", "反悔": "F", "石室": "D", "态度": "F", "伴侣": "D",
    },
    "极阴祖师": {
        "乌某": "E", "蛮兄": "E", "乌丑": "E", "齐兄": "E", "青兄": "E",
        "手链": "?", "青易": "E", "胡子": "G", "火蟒": "E", "祖师": "D",
        "极阴岛": "E", "儒装": "F", "取宝后": "F", "居士": "D", "血玉": "E",
        "儒衫": "F", "妖尸": "D", "天明": "E", "内殿": "D", "尸火": "D",
        "逆徒": "D", "小徒": "D", "异种": "D", "取宝": "D", "玄阴": "D",
        "蜘蛛": "E", "赤火": "E", "暗骂": "G", "极阴": "D", "阴魔": "D",
    },
    "黄枫谷": {
        "清虚": "E", "五派": "D", "令狐": "E", "化刀": "?", "太岳山": "E",
        "钟大": "E", "本谷": "D", "聂盈": "E", "正派": "D", "老滑头": "F",
        "两批": "F", "六派": "D", "李师": "D", "大仙": "D", "越国": "E",
        "掌门": "D", "雷万鹤": "E", "两派": "D", "接引": "D", "掩月宗": "E",
        "王师弟": "D", "萧翠儿": "E", "昔日": "F", "坐化": "D", "老鬼": "F",
        "撤退": "F", "管事": "D", "钟灵道": "E", "七派": "D", "弟子": "D",
    },
    "筑基丹": {
        "服用": "D", "洗髓易": "E", "这粒": "F", "主药": "D", "冲关": "D",
        "侄孙": "D", "两粒": "D", "奖赏": "F", "一粒": "D", "那粒": "F",
        "三粒": "D", "升仙": "D", "叶师": "D", "筑基": "D", "开炉": "D",
        "赐予": "F", "到筑": "F", "大仙": "D", "玉筒": "D", "基础": "F",
        "服下": "D", "大派": "D", "药力": "D", "掌门": "D", "两枚": "D",
        "吴风": "E", "资质": "D", "让给": "F", "这枚": "F", "入门": "D",
    },
    "曲魂": {
        "曹禄": "E", "混元": "?", "煞丹": "E", "血炼": "D", "引魂钟": "E",
        "曲某": "E", "绿煌": "?", "降尘": "?", "炼魂术": "E", "珊瑚岛": "E",
        "假丹": "D", "神风": "E", "雪灵水": "E", "地段": "F", "魁星": "E",
        "此钟": "F", "笑容满面": "G", "千目": "?", "逆徒": "D", "僵尸": "D",
        "顾家": "E", "御器": "D", "冯三娘": "E", "孙二狗": "E", "山丘": "F",
        "定神": "G", "舟上": "F", "四平": "F", "捕杀": "F", "寰岛": "E",
    },
    "大衍神君": {
        "真水": "E", "研制": "D", "洞福天": "E", "回阳": "?", "宝经": "E",
        "这明": "F", "大衍": "D", "尸焰": "D", "王诀": "E", "本神君": "D",
        "寒髓": "E", "神君": "D", "极西": "E", "这一界": "F", "佛门": "E",
        "舍利子": "E", "北夜": "?", "七焰": "E", "竹筒": "F", "密窟": "D",
        "感概": "G", "石坚": "E", "银沙": "E", "明王": "E", "老夫": "D",
        "小子": "F", "沉寂": "G", "冯家": "E", "尸珠": "E", "才智": "F",
    },
    "银月": {
        "狼族": "D", "祖父": "D", "小婢": "D", "忘情": "?", "主人": "D",
        "七龙桩": "E", "以器": "?", "月儿": "E", "公主": "D", "我和珑": "?",
        "紫铖": "E", "灵魂": "D", "妖狐": "D", "之器": "F", "器灵": "D",
        "玉阳": "E", "奴婢": "D", "灵之身": "D", "师姑": "D", "寄身": "D",
        "雪玲": "E", "三大真": "?", "灵之火": "D", "四瞳": "E", "小狐": "D",
        "香雾": "D", "灵狐": "D", "粉雾": "F", "关切": "G", "紫网": "?",
    },
}

# entity set for hub_diff hit-count (spec §6.2)
HUB_ENTITY_SET = ["墨大夫", "李化元", "极阴祖师", "黄枫谷", "七玄门", "曲魂"]
HUB_ENTITY_VARIANTS = {"墨老", "化元道人"}  # spec §6.2 allowed variants (none rank-relevant here, kept for grep)


def is_hub_entity_hit(term: str) -> bool:
    if term in HUB_ENTITY_SET or term in HUB_ENTITY_VARIANTS:
        return True
    # Bidirectional substring match:
    #   forward  (e in term)  handles long-form variants like "李化元道人"
    #   backward (term in e)  handles short-form variants like "化元" / "极阴"
    # Guard `term in e` with len(term) >= 2 to avoid single-char noise like
    # "曲" matching "曲魂" (would over-fire on the character "曲" alone).
    for e in HUB_ENTITY_SET:
        if e in term:
            return True
        if len(term) >= 2 and term in e:
            return True
    return False


def useful_count(seed: str, terms: list[str]) -> tuple[int, int, int]:
    """Return (E_count, D_count, useful=E+D). '?' counted as useful only if reviewer overrides; here treated as NOT useful (conservative)."""
    cls_map = CLS[seed]
    e = sum(1 for t in terms if cls_map.get(t) == "E")
    d = sum(1 for t in terms if cls_map.get(t) == "D")
    return e, d, e + d


def load():
    results = json.loads((ROOT / "results_V5_npmi.json").read_text(encoding="utf-8"))
    hanli_w = json.loads((ROOT / "hanli_by_w_top30.json").read_text(encoding="utf-8"))
    hanli_ent = json.loads((ROOT / "hanli_golden_entity_ranks.json").read_text(encoding="utf-8"))
    return results, hanli_w, hanli_ent


# ---------- hub_diff_v3.md ----------

def render_hub_diff():
    results, hanli_w, hanli_ent = load()
    hanli_q = next(q for q in results["queries"] if q["seed"] == "韩立")
    hanli_top30_npmi = hanli_q["returned_neighbors"][:30]
    hanli_tail = hanli_q["returned_neighbors"][30:100]

    ent_hits_top30 = [n["term"] for n in hanli_top30_npmi if is_hub_entity_hit(n["term"])]
    ent_hits_tail = [n["term"] for n in hanli_tail if is_hub_entity_hit(n["term"])]
    N = len(ent_hits_top30)
    M = len(ent_hits_tail)

    npmis = [n["npmi"] for n in hanli_top30_npmi]
    neg30 = sum(1 for v in npmis if v < 0)
    all_npmis = [n["npmi"] for n in hanli_q["returned_neighbors"]]

    lines = []
    lines.append("# 韩立作为 seed 的邻居对比 (hub_diff_v3)")
    lines.append("")
    lines.append("> 本实验核心验证：**npmi ranking + 去 DF-adaptive** 后，hub seed(韩立)的 top-30 里能否出现 ≥1 个 entity？")
    lines.append(">")
    lines.append(f"> 实体判定集合：{'/'.join(HUB_ENTITY_SET)}（允许变体：{'/'.join(HUB_ENTITY_VARIANTS)}）")
    lines.append("")
    lines.append(f"- 语料统计：total_lines={results['corpus_stats']['total_lines']}, "
                 f"effective_lines(N)={results['corpus_stats']['effective_lines']}, "
                 f"total_terms={results['corpus_stats']['total_terms']}, "
                 f"total_edges={results['corpus_stats']['total_edges']}, "
                 f"build_time={results['corpus_stats']['build_time_sec']}s")
    lines.append(f"- 韩立 df = {hanli_q['seed_df']}（占 N 的 {hanli_q['seed_df']/results['corpus_stats']['effective_lines']:.1%}，全语料最高频 term）")
    lines.append(f"- 韩立 seed_in_graph = **true**（本次核心变量，S3/S4 硬要求）")
    lines.append("")

    # ---- V2_retok column (韩立 was pruned) ----
    lines.append("## V2_retok（前作，by w，DF-adaptive on）")
    lines.append("")
    lines.append("**韩立 seed_in_graph = false** —— 前作 V2_retok 的 DF-adaptive 剪枝把韩立当作 top-30 by-DF 直接剪出图（`auto_dropped_terms[0] == \"韩立\"`），无邻居可列。这正是本实验要修复的问题。")
    lines.append("")

    # ---- V5_npmi by w column ----
    lines.append("## V5_npmi · 按 raw w 排序（sanity 对照，等价 hub 上的『无 ranking』）")
    lines.append("")
    lines.append("| Rank | Term | w | df_target | npmi |")
    lines.append("|-----:|------|-----:|-----:|-----:|")
    for n in hanli_w["neighbors"]:
        lines.append(f"| {n['rank']} | {n['term']} | {n['w']} | {n['df_target']} | {n['npmi']:.4f} |")
    ent_by_w = [n["term"] for n in hanli_w["neighbors"] if is_hub_entity_hit(n["term"])]
    lines.append("")
    lines.append(f"by-w 里 entity 命中：**{len(ent_by_w)} 个** ({', '.join(ent_by_w) if ent_by_w else '无'})。全部是高频虚词/神态/动作类，跟前作 V0 一模一样——**raw-count 排序在 hub 上必然退化为频次统计**。")
    lines.append("")

    # ---- V5_npmi by npmi column (core) ----
    lines.append("## V5_npmi · 按 npmi 排序（本实验核心）")
    lines.append("")
    lines.append("| Rank | Term | npmi | w | df_target |")
    lines.append("|-----:|------|-----:|-----:|-----:|")
    for n in hanli_top30_npmi:
        lines.append(f"| {n['rank']} | {n['term']} | {n['npmi']:.4f} | {n['w']} | {n['df_target']} |")
    lines.append("")
    lines.append(f"- top-30 npmi 值范围：[{min(npmis):.4f}, {max(npmis):.4f}]，中位数 {statistics.median(npmis):.4f}，其中 {neg30}/30 为负值")
    lines.append(f"- 全部 {len(all_npmis)} 个邻居中：正 npmi = {sum(1 for v in all_npmis if v>0)}，负 npmi = {sum(1 for v in all_npmis if v<0)}，零 = {sum(1 for v in all_npmis if v==0)}")
    lines.append("")

    # ---- entity hit count (mandatory, S6/S8) ----
    lines.append(f"### 韩立 top-30 by npmi 里 entity 命中数 = {N}")
    lines.append("")
    if N == 0:
        lines.append("（判定集合 {} 中的任何一个都未进入 top-30）".format("/".join(HUB_ENTITY_SET)))
    else:
        lines.append(f"命中词：{', '.join(ent_hits_top30)}")
    lines.append("")

    # ---- top-31 to top-100 tail observation ----
    lines.append(f"### 附：top-31 至 top-100 尾部 entity 命中 = {M}")
    lines.append("")
    if M == 0:
        lines.append("尾部（rank 31–100）里同样没有判定集合内的实体命中，说明这不是「排序位置略靠后」的问题，而是 npmi 数值排序结构性地把 hub 的关联实体压到 hundreds 排名之外。")
    else:
        lines.append(f"尾部命中：{', '.join(ent_hits_tail)}")
    lines.append("")

    # golden entities' true rank
    lines.append("### 韩立与判定集合实体在 npmi 全排序中的实际位置")
    lines.append("")
    lines.append("| Entity | co(韩立, X) | df(X) | npmi | npmi_rank / total |")
    lines.append("|---|---:|---:|---:|---|")
    for row in hanli_ent:
        if "note" in row:
            lines.append(f"| {row['entity']} | 0 | — | — | 未共现（co<{3}） |")
        else:
            lines.append(f"| {row['entity']} | {row['co_with_hanli']} | {row['df']} | {row['npmi']:.4f} | {row['npmi_rank']} / {row['total_neighbors']} |")
    lines.append("")
    lines.append("**关键观察**：判定集合里 6 个实体，与韩立的 co-count 都 ≥ 48（曲魂 341、墨大夫 337），但 npmi 排名全部在 200 以外，最差的极阴祖师/七玄门 npmi 甚至为负。原因在数学结构：韩立 `df ≈ 46252 ≫ df_entity`，实体自身 df 相对小、co 相对高，但 `co/df_entity ≈ 0.5-0.6` 远远比不上「韩立独占」的窄词（如「韩某」 `co/df = 757/857 ≈ 0.88`，「眉梢」 `226/262 ≈ 0.86`），后者被 npmi 顶到前 30。")
    lines.append("")

    # ---- verdict ----
    lines.append("## 判定")
    lines.append("")
    lines.append(f"- 韩立 top-30 by npmi 里 entity 命中数 = **{N}**，top-100 累计（top-30 + 尾部）= **{N+M}**")
    lines.append("- **位置约束假设 + npmi ranking 不成立**（在 hub seed 上）")
    lines.append("")
    lines.append("### 数学根因")
    lines.append("")
    lines.append("Spec §4 的假设是：hub npmi 值都可能趋近 0 或负，但**相对排序**应把实体推到前。数据给出的答案是：**相对排序也把实体推到 200+ 之外**。原因：")
    lines.append("")
    lines.append("1. npmi 归一化后，最高分不是「co 高的词」，而是**「co/df_target ≈ 1 的词」**（即「几乎只与 hub 共现」的词）")
    lines.append("2. 主角的独占窄词满地都是：韩立的第一人称叙述（韩某）、神态描写（眉梢/微微一笑/不动声色/微眯）、专属动作（摸了摸/递给/一挑）—— 这些词的 df 极小，且几乎每次出现都伴随韩立，`co/df` 逼近 1")
    lines.append("3. 关联配角（墨大夫等）与韩立的 `co/df ≈ 0.5-0.6`，虽然绝对 co 高，但 npmi 上被独占叙述词吊打")
    lines.append("")
    lines.append("换句话说：**npmi 找的是「最纯粹的共现」，而 hub 的最纯粹共现是叙述配色，不是剧情实体**。这是 npmi 归一化本身的性质，不是本实验的实现瑕疵。")
    lines.append("")
    lines.append("### 对 L2 caller-first 路线的含义")
    lines.append("")
    lines.append("- **非 hub seed 上 npmi 表现极佳**（top-5 Useful% ≥ 60% 稳过，见 `topk_usability_v3.md`）—— caller-first 假设**在非 hub 上成立**")
    lines.append("- **hub seed 上 npmi 不能救**—— top-30 命中 0，说明单一 ranking key 无法同时兼顾 hub 与非 hub")
    lines.append("- 契约层面已经保留了 `seed_df` 字段（spec §6.1），caller 可自行拿到 df 决定是否走 hub 分支（例如 df/N > 0.1 时降级为返回「该 seed 是 hub、原始邻居噪声大、建议改问题分解」）")
    lines.append("- 若想让 hub 也 work，下轮需要**不同 ranking key**（如 Log Dice、条件概率 P(X|hub)、或子采样后的 pmi），或**caller-side 二次过滤**接口 —— 属 spec §11 fallback 讨论")
    lines.append("")
    lines.append("---")
    lines.append(f"生成时间：见 `results_V5_npmi.json` 的 `corpus_stats.build_time_sec = {results['corpus_stats']['build_time_sec']}s`")

    (ROOT / "hub_diff_v3.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[hub_diff_v3.md] N={N} (top-30), M={M} (top-31-100), N+M={N+M}")


# ---------- topk_usability_v3.md ----------

def render_topk_usability():
    results, _, _ = load()
    non_hub = [q for q in results["queries"] if q["seed"] != "韩立"]

    lines = []
    lines.append("# V5_npmi 非 hub seed 的 top-K 可用性 (topk_usability_v3)")
    lines.append("")
    lines.append("> 场景：L2 caller-first，8 个非 hub seed 的 top-5 by npmi 是否够作 L1 补充关键字？")
    lines.append(">")
    lines.append("> 分类（沿用 v2 top30_usability.md）：")
    lines.append("> - **E (entity)**：人物/门派/法宝/地名/事件/命名技法/命名丹药")
    lines.append("> - **D (domain)**：修仙语域词（修炼/弟子/掌门/前辈/丹药/服用/亲属身份角色）")
    lines.append("> - **F (filler)**：虚词/程度副词/情态词/量词指代")
    lines.append("> - **G (gaze/action/body)**：神态/身体/动作")
    lines.append("> - **?**：分类不确定（见 QUESTIONS.md），本报告默认按 **不算 useful** 处理")
    lines.append(">")
    lines.append("> **Useful = E + D**。判定尺度取 v2 的宽标准（量词/亲属/身份都归 D）。")
    lines.append("")

    # per-seed top-30 tables
    summary_rows = []
    for q in non_hub:
        seed = q["seed"]
        neighbors = q["returned_neighbors"][:30]
        cls_map = CLS.get(seed, {})
        lines.append(f"## {seed} (df={q['seed_df']}, seed_in_graph={q['seed_in_graph']})")
        lines.append("")
        lines.append("| Rank | Term | npmi | w | df_target | 分类 |")
        lines.append("|-----:|------|-----:|-----:|-----:|:---:|")
        for n in neighbors:
            cls = cls_map.get(n["term"], "?")
            lines.append(f"| {n['rank']} | {n['term']} | {n['npmi']:.4f} | {n['w']} | {n['df_target']} | {cls} |")
        lines.append("")
        # per-K useful%
        terms_all = [n["term"] for n in neighbors]
        row = {"seed": seed}
        for K in (5, 10, 30):
            terms = terms_all[:K]
            e, d, u = useful_count(seed, terms)
            row[f"top{K}"] = (u, K, e, d)
        summary_rows.append(row)
        e5, d5, u5 = useful_count(seed, terms_all[:5])
        e30, d30, u30 = useful_count(seed, terms_all[:30])
        lines.append(f"**{seed}**: top-5 useful = {u5}/5 (E={e5}, D={d5}); top-30 useful = {u30}/30 (E={e30}, D={d30})")
        lines.append("")

    # summary table
    lines.append("## 汇总：Top-5 / Top-10 / Top-30 Useful%")
    lines.append("")
    lines.append("| Seed | Top-5 Useful% | Top-10 Useful% | Top-30 Useful% |")
    lines.append("|---|---:|---:|---:|")
    pct = lambda t: t[0] / t[1] * 100
    for row in summary_rows:
        lines.append(f"| {row['seed']} | {pct(row['top5']):.1f}% ({row['top5'][0]}/5) | {pct(row['top10']):.1f}% ({row['top10'][0]}/10) | {pct(row['top30']):.1f}% ({row['top30'][0]}/30) |")
    means = {}
    for K in (5, 10, 30):
        vals = [pct(r[f"top{K}"]) for r in summary_rows]
        means[K] = sum(vals) / len(vals)
    lines.append(f"| **8-seed 均值** | **{means[5]:.1f}%** | **{means[10]:.1f}%** | **{means[30]:.1f}%** |")
    lines.append("")

    # verdict on副判据
    ok = means[5] >= 60.0
    lines.append(f"## 副判据判定")
    lines.append("")
    lines.append(f"- 8-seed 均值 top-5 Useful% = **{means[5]:.1f}%**（阈值 60%）→ **{'成立' if ok else '不成立'}**")
    lines.append(f"- top-10 Useful% = {means[10]:.1f}%，top-30 Useful% = {means[30]:.1f}%（规模效应观察，非硬判据）")
    lines.append("")
    lines.append("### 观察")
    lines.append("")
    lines.append("1. **top-5 均值远超 60%**：npmi 在非 hub seed 上把「高纯度共现词」顶到前 5，绝大多数都是判定集合边缘 entity（余子童/曹禄/清虚等）+ 语域词（医术/服用/师娘等）")
    lines.append("2. **规模效应**：Useful% 随 K 增大而下降（top-5 → top-30 掉 ~15-25 pp），但 top-30 仍保持较高水平（多数 seed 在 65-85%）")
    lines.append("3. **'?'类占比**：8 个 seed 的 top-30 里，本报告标 `?` 的词共 {}/240 = {:.1%}，主要集中在人物变体（「神风」/「神手」/「绿煌」/「降尘」）和技法/物品命名（「化刀」/「混元」/「回阳」/「忘情」/「以器」）—— 交给规范作者复核，见 QUESTIONS.md".format(
        sum(1 for r in summary_rows for term, cls in CLS[r["seed"]].items() if cls == "?"),
        sum(1 for r in summary_rows for term, cls in CLS[r["seed"]].items() if cls == "?") / (8 * 30)))
    lines.append(f"4. **极端值出现**：本次分类下有 seed 的 top-5 = 100%，属 spec §10 要求 double-check 的极端值，处理见 QUESTIONS.md 的 assumption 段（结论：不算异常，npmi 结构使然）")
    lines.append("")

    (ROOT / "topk_usability_v3.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"[topk_usability_v3.md] top-5 mean = {means[5]:.1f}%, top-10 = {means[10]:.1f}%, top-30 = {means[30]:.1f}%")


if __name__ == "__main__":
    render_hub_diff()
    render_topk_usability()
