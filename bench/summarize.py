"""Read the test results (bench/work/t*.json) and score them against the pass marks in PLAN.md."""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from common import WORK


def load(name: str):
    path = WORK / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else []


def forecast() -> list[str]:
    rows = [r for r in load("t3_forecast") if "app_wape" in r]
    skipped = [r for r in load("t3_forecast") if "skipped" in r]
    if not rows:
        return ["Forecast: no results"]
    df = pd.DataFrame(rows)
    out = ["Forecast (30-day windows, app vs 'same weekday last week'):"]
    for series, g in df.groupby("series", sort=False):
        out.append(f"  {series:28s} windows {len(g)}  app WAPE {100 * g.app_wape.mean():5.1f}%  "
                   f"baseline {100 * g.naive_wape.mean():5.1f}%  avg-4-weeks {100 * g.avg4_wape.mean():5.1f}%  "
                   f"app better in {(g.app_wape <= g.naive_wape).sum()}/{len(g)}  "
                   f"80% range held {100 * g.range_coverage.mean():.0f}% of days")
    better = (df.app_wape <= df.naive_wape).mean()
    gap = (df.claimed_improvement_pct - df.real_improvement_pct).abs()
    cover = df.range_coverage.mean()
    out.append(f"  PASS MARKS: app at least as good in >= 2/3 of windows: {100 * better:.0f}% "
               f"-> {'PASS' if better >= 2 / 3 else 'FAIL'}")
    out.append(f"              claimed improvement within 10 points of real: {100 * (gap <= 10).mean():.0f}% of windows "
               f"(median gap {gap.median():.0f} points) -> {'PASS' if (gap <= 10).mean() >= 2 / 3 else 'FAIL'}")
    out.append(f"              80% range held 70-90% of days: {100 * cover:.0f}% -> "
               f"{'PASS' if 0.7 <= cover <= 0.9 else 'FAIL'}")
    out.append(f"  also: app vs avg of last 4 same weekdays: app better in {100 * (df.app_wape <= df.avg4_wape).mean():.0f}% of windows")
    if skipped:
        out.append(f"  {len(skipped)} window(s) refused for too little history (counted as neither pass nor fail)")
    return out


def churn() -> list[str]:
    rows = load("t4_churn")
    out = ["Churn (app's scores vs who really stopped buying in the hidden period):"]
    ok = []
    for r in rows:
        if "skipped" in r:
            out.append(f"  {r['dataset']:7s} h={r['h']:2d} cutoff {r['cutoff']}: refused - {r['skipped'][:110]}")
            continue
        if "real_auc" not in r:
            out.append(f"  {r['dataset']:7s} h={r['h']:2d} cutoff {r['cutoff']}: no outcome variation")
            continue
        beat = r["real_auc"] >= r["real_baseline_auc"] + 0.02
        honest = abs(r["claimed_auc"] - r["real_auc"]) <= 0.05
        ok.append((beat, honest))
        out.append(f"  {r['dataset']:7s} h={r['h']:2d} cutoff {r['cutoff']}: model {r['model']:8s} scored {r['scored']:5d}  "
                   f"real AUC {r['real_auc']:.3f} vs recency {r['real_baseline_auc']:.3f}  claimed {r['claimed_auc']:.3f}  "
                   f"top20% caught {r['real_top20']:.0f}% vs {r['real_baseline_top20']:.0f}%  "
                   f"-> beats baseline {'yes' if beat else 'NO'}, claim honest {'yes' if honest else 'NO'}")
    if ok:
        out.append(f"  PASS MARKS: beats recency by >= 0.02 AUC in {sum(b for b, _ in ok)}/{len(ok)}; "
                   f"claimed AUC within 0.05 in {sum(h for _, h in ok)}/{len(ok)}")
    return out


def basket() -> list[str]:
    rows = load("t5_basket")
    out = ["Bought together:"]
    for r in rows:
        if "pairs_found" in r:
            out.append(f"  {r['dataset']:7s} first half found {r['pairs_found']} pairs; still lift > 1 in second half: "
                       f"{r['hold_lift_gt_1_pct']}% of all, {r.get('testable_lift_gt_1_pct')}% of the "
                       f"{r.get('pairs_testable')} whose products were still sold "
                       f"(still strong & significant: {r['hold_lift_ge_1_2_and_z3_pct']}%) "
                       f"-> {'PASS' if (r['hold_lift_gt_1_pct'] or 0) >= 80 else 'FAIL'} on all pairs")
    for name in sorted({r["dataset"] for r in rows}):
        fp = [r["false_pairs"] for r in rows if r["dataset"] == name and "false_pairs" in r]
        if fp:
            out.append(f"  {name:7s} shuffled data (nothing real to find): false pairs {fp}, mean {np.mean(fp):.1f} "
                       f"-> {'PASS' if np.mean(fp) <= 1 else 'FAIL'}")
    return out


def anomaly_days() -> list[str]:
    rows = load("t6_anomaly_days")
    if not rows:
        return ["Unusual days: no results"]
    df = pd.DataFrame(rows)
    out = ["Unusual days (10 injected per run, 3 runs per dataset):"]
    for (name, method), g in df.groupby(["dataset", "method"], sort=False):
        found = (g.spikes_found + g.drops_found).sum() / g.injected.sum()
        out.append(f"  {name:7s} {method:11s} found {100 * found:3.0f}% (spikes {g.spikes_found.sum()}/{len(g) * 5}, "
                   f"drops {g.drops_found.sum()}/{len(g) * 5})  other-day flags {g.other_flags_after.mean():.1f} "
                   f"vs {g.flags_before.mean():.0f} before, over {g.days.iloc[0]} days")
    app = df[df.method == "app"]
    found = (app.spikes_found + app.drops_found).sum() / app.injected.sum()
    calm = (app.other_flags_after <= app.flags_before).mean()
    out.append(f"  PASS MARKS: found >= 80%: {100 * found:.0f}% -> {'PASS' if found >= 0.8 else 'FAIL'}; "
               f"no extra flags elsewhere in {100 * calm:.0f}% of runs -> {'PASS' if calm == 1 else 'FAIL'}")
    return out


def lines() -> list[str]:
    rows = load("t7_unusual_lines")
    out = ["Unusual sale lines:"]
    for r in rows:
        if r["test"] == "known keying errors":
            for e in r["errors"]:
                out.append(f"  retail known error: {e['product']} x{e['quantity']:.0f} (product sold {e['product_sales']} "
                           f"times) -> {'reported' if e['reported'] else 'NOT reported'}")
        else:
            out.append(f"  {r['dataset']:7s} price typos found {r['found']}/{r['injected']} {r['found_by_factor']} "
                       f"(reports at most 25 lines; {r['reported_not_injected']} other lines reported) -> "
                       f"{'PASS' if r['found'] >= 0.8 * r['injected'] else 'FAIL'}")
    return out


def segments() -> list[str]:
    rows = load("t8_segments")
    out = ["Customer segments (vs spend in the next 90 days):"]
    for r in rows:
        segs = ", ".join(f"{s['segment']} {s['customers']} cust. spend {s['future_spend']:.0f}, "
                         f"{s['bought_again_pct']:.0f}% bought" for s in r["segments"])
        out.append(f"  {r['dataset']:7s} {segs}")
        out.append(f"          better groups spend more than worse: {'PASS' if r['better_beat_worse'] else 'FAIL'}; "
                   f"rank corr. with future spend: segments {r['spearman_segment_rank']:.3f} vs "
                   f"RFM quintiles {r['spearman_rfm_quintiles']:.3f}")
    return out


def reading() -> list[str]:
    out = ["Reading and mapping the original files:"]
    for r in load("t1_reading"):
        head = f"  {r['dataset']:7s} {r.get('sheet') or '':15s} profiles {'on ' if r['profiles'] else 'off'}"
        if "read_error" in r:
            out.append(f"{head} READ ERROR {r['read_error'][:120]}")
            continue
        ms = r["mapping_score"]
        line = f"{head} mapping {ms['score']} wrong {ms['wrong']} missing {ms['missing']}"
        if "completed_by_hand" in r:
            line += " (could not load as suggested)"
        if "clean_error" in r:
            line += f" CLEAN ERROR {r['clean_error'][:100]}"
        elif "rows_kept" in r:
            line += (f"; kept {r['rows_kept']}/{r['rows_in_file']}, rejected {r['rejected_reasons']}; "
                     f"revenue diff {r['revenue_diff_pct']}%, rows wrong {r['rows_revenue_wrong']}, "
                     f"dates wrong {r['rows_date_wrong']}")
            if "kept_non_product_lines" in r:
                line += f"; non-product lines kept {r['kept_non_product_lines']} ({r['kept_non_product_revenue']:,.0f})"
        out.append(line)
    out.append("Messy versions of the same rows:")
    for r in load("t2_messy"):
        if "error" in r:
            out.append(f"  {r['dataset']:7s} L{r['level']} ERROR {r['error'][:120]}")
        elif r["level"] == 0:
            out.append(f"  {r['dataset']:7s} L0 kept {r['rows_kept']} revenue {r['revenue']:,.2f}")
        else:
            out.append(f"  {r['dataset']:7s} L{r['level']} same rows {r.get('same_rows')}, revenue diff "
                       f"{r.get('revenue_diff_pct')}%, dates wrong {r.get('dates_wrong')}, "
                       f"rejected {r['rejected_reasons']}")
    return out


if __name__ == "__main__":
    for part in (reading, forecast, churn, basket, anomaly_days, lines, segments):
        print("\n".join(part()))
        print()
