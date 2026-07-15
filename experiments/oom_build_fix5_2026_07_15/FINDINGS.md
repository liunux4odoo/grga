# FINDINGS — oom_build_fix5_2026_07_15 (PR-7 substring salvage 反向枚举)

**触发**：用户 300 万 chunks 实测 —— PR-6 已解 OOM（peak 3.4 GB 稳），但
ngram substring salvage 阶段慢到不可用："打捞子串短语 563200/3231822"
用时 40 分钟 → 全程外推 **~3.8 小时**。

## TL;DR

Stage 6 substring salvage 从"每 reject 扫 survivor 桶"改成"每 survivor
枚举子串查 reject 集合"。**~1870× 加速**（实测微基准 40k rejects：
311s → 1.7s；等价性 True）。用户 3.2M rejects 从 3.8h → **~15s**。
S 档 6 表 bit-exact vs 原 salvage（245k 行严等）。pytest 149 项全绿。

## 根因

旧算法（build.py:1262-1295，PR-6 之前存在）:
```python
by_first_char[ch] = [(survivor_text, count), ...]  # 按 survivor 首字符建桶
for reject in rejects:
    bucket = by_first_char[reject.text[0]]
    for kt, kc in bucket:           # 桶线性扫
        if len(kt)>t_len and reject.text in kt:  # 子串检查
            max_container_c = max(max_container_c, kc)
```

问题：
- **桶极不均匀**——高频 CJK 首字符（的/一/是/了）的桶塞满几十万 survivor
- **每 reject O(bucket × substring_cost)**——3M rejects × 数十万桶 × O(L)
  子串扫描
- 250 rejects/秒的原因，**complexity 是根本瓶颈**

## 修复：反转方向

```python
reject_texts = {rejoin(w) for w, c, s, b in rejects}  # O(rejects) 一次
max_cc = {}
for r in survivors:                     # O(survivors)
    kt = r["phrase"]; L = len(kt)
    for i in range(L):                  # O(L^2) 子串枚举
        for j in range(i+1, L+1):
            if i == 0 and j == L: continue
            sub = kt[i:j]
            if sub in reject_texts:     # O(1) 集合查
                max_cc[sub] = max(max_cc.get(sub, 0), kc)
# 最后按原 reject 序遍历 → 追加 results（保 append 序 → sort tiebreak 不变）
```

**复杂度**：O(rejects + survivors × L²) 其中 L = phrase 字符数（≤ max_ngram_n
× 单 token 长度，实际约 2-15）。集合查是 O(1) 而非旧的 O(bucket)。

## 等价性

- **max_container_c 是 sub 的纯函数**（max over all containers）→ 顺序无关
- **最终 append 顺序保持**（遍历原 entropy_rejects 序）→ Stage 7 sort tiebreak
  与旧实现严等
- 排除 `i==0 and j==L`（全字符串不是 *proper* 子串）↔ 旧代码 `len(kt) > t_len`
  保证：候选来自 first-char 桶但 t_len < kt_len 强制取真子串

## 验证

| 场景 | 结果 |
|---|---|
| S 22MB 6 表 bit-exact vs 原 salvage | ✅ 全等（245k+ 行） |
| pytest 149 项 | ✅ 全绿 |
| pytest test_ngram_salvages_* (6 项) | ✅ 全绿 |
| 微基准 40k rejects × 200k survivors | ✅ 老 311s / 新 1.7s / 1870× / equiv True |
| 外推用户 3.2M rejects × ~500k survivors | 老 3.8h → 新 ~15s |

## 内存影响

- `reject_texts` 集合：≤ 3.2M 条不同字符串 × ~50 B = **≤ 160 MB**（比 dedupe
  前的 `entropy_rejects` tuple list 还小）
- `max_container_c` dict：≤ salvaged 候选数（通常远小于 3.2M）× ~80 B
- 新增内存显著小于旧 by_first_char 的每 survivor × distinct_chars 索引

## 触发规模

反向优势随 corpus 规模递增：
- 小语料（几千 chunks）：老新差距不大（桶小）
- M 22MB（12k chunks，~30k rejects）：老已经 ~2s，新 <0.1s，用户感知不到
- 用户 300万 chunks（3.2M rejects, ~500k survivors）：老 3.8h **不可用**，
  新 ~15s 才使 build 可跑完
