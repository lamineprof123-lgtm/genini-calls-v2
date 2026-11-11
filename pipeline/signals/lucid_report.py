"""All-day/all-account paper trade confluence report; no network or account mutation."""
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
import json
import os
from zoneinfo import ZoneInfo


def read_records(root, name):
    rows, errors = [], []
    for path in sorted(Path(root).glob(f'*/{name}')):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except (ValueError, TypeError):
                errors.append(f'{path}:{n}')
    return rows, errors


def stats(rows):
    vals = [float(r.get('net') or 0) for r in rows]
    wins, losses = [v for v in vals if v > 0], [v for v in vals if v < 0]
    n = len(vals)
    return dict(count=n, wins=len(wins), losses=len(losses), breakeven=n-len(wins)-len(losses),
                win_rate=round(len(wins)/n, 6) if n else None,
                net_pnl=round(sum(vals), 2),
                avg_win=round(sum(wins)/len(wins), 2) if wins else None,
                avg_loss=round(sum(losses)/len(losses), 2) if losses else None,
                expectancy=round(sum(vals)/n, 2) if n else None,
                low_confidence=n < 20)


def confidence_band(c):
    if c is None:
        return 'unknown'
    return '<0.6' if c < .6 else '0.6-0.7' if c < .7 else '0.7-0.8' if c < .8 else '>=0.8'


def label(value):
    return 'yes' if value is True else 'no' if value is False else 'unknown' if value is None else str(value)


def skip_category(why):
    why = (why or '').lower()
    if why == 'session_blocked': return 'session_blocked'
    if 'cannot reach' in why: return 'sizing_unreachable'
    if 'chase' in why: return 'chase'
    if 'confidence' in why: return 'confidence'
    if 'outside lucid' in why: return 'outside_lucid_session'
    if 'day locked' in why: return 'day_locked'
    if 'already in a position' in why: return 'already_position_or_pending'
    if 'correlated' in why: return 'correlated_anti_hedge'
    if 'expir' in why: return 'pending_expired'
    return why or 'unknown'


def build_report(trades, events):
    groups = {k: defaultdict(list) for k in ('session', 'value_area_position', 'cvd_agrees', 'rvol_spike',
                                           'vwap_aligned', 'ema50_aligned', 'vwap_ema50_aligned',
                                           'confidence_band', 'symbol', 'exit_reason')}
    combos = defaultdict(list)
    for row in trades:
        t = row.get('telemetry') or {}
        g = t.get('gemini') or row.get('gemini') or {}
        a, b = t.get('vwap_aligned_with_side'), t.get('ema50_aligned_with_side')
        combined = None if a is None or b is None else a and b
        keys = dict(session=t.get('session') or (row.get('gemini') or {}).get('session'),
                    value_area_position=t.get('price_vs_value_area'), cvd_agrees=t.get('cvd_agrees_with_side'),
                    rvol_spike=t.get('rvol_spike'), vwap_aligned=a, ema50_aligned=b, vwap_ema50_aligned=combined,
                    confidence_band=confidence_band(g.get('confidence')), symbol=row.get('symbol'),
                    exit_reason=row.get('reason'))
        for kind, key in keys.items(): groups[kind][label(key)].append(row)
        combos[(label(keys['session']), label(keys['value_area_position']), label(keys['cvd_agrees']))].append(row)
    combinations = [dict(session=k[0], value_area_position=k[1], cvd_agrees=k[2], **stats(rs))
                    for k, rs in combos.items()]
    combinations.sort(key=lambda r: (-r['net_pnl'], -r['count'], r['session']))
    skips = [r for r in events if r.get('event') == 'entry_skipped']
    return dict(scope='All days and all accounts; paper trades only; net P&L after round-trip commission',
                summary=stats(trades),
                groups={kind: {k: stats(rs) for k, rs in sorted(bucket.items())} for kind, bucket in groups.items()},
                top_combinations=combinations,
                skips=dict(count=len(skips), by_reason=dict(Counter(skip_category(r.get('why')) for r in skips)),
                           exact_reasons=dict(Counter(r.get('why') or 'unknown' for r in skips))),
                definitions=dict(win='net > 0; breakeven is not a win or a loss',
                                 avg_loss='signed negative net P&L', expectancy='total net / closed trade count',
                                 low_confidence='n < 20; descriptive only, not proof of an edge',
                                 cvd_agrees='bullish label for BUY, bearish for SELL; neutral label uses slope direction',
                                 missing_telemetry='unknown, never backfilled from later bars'))


def format_report(r):
    lines = ['# Lucid paper confluence report', '', r['scope'], '',
             f"Updated: {r.get('generated_at_dubai', 'on demand')} (Dubai UTC+4)", '',
             '**Every bucket with n < 20 is LOW CONFIDENCE. Descriptive only; no proven edge.**', '']
    def table(items):
        out = ['| Group | n | Win rate | Net P&L | Avg win | Avg loss | Expectancy | Sample |',
               '|---|---:|---:|---:|---:|---:|---:|---|']
        for name, s in items:
            money = lambda v: 'n/a' if v is None else f'${v:,.2f}'
            wr = 'n/a' if s['win_rate'] is None else f"{s['win_rate']:.1%}"
            name = str(name).replace('|', '/')
            out.append(f"| {name} | {s['count']} | {wr} | {money(s['net_pnl'])} | {money(s['avg_win'])} | {money(s['avg_loss'])} | {money(s['expectancy'])} | {'LOW CONFIDENCE' if s['low_confidence'] else 'n >= 20'} |")
        return out
    lines += table([('All closed trades', r['summary'])])
    if not r['summary']['count']: lines += ['', 'No closed paper trades yet. Win rate/expectancy are unavailable, not zero.']
    for kind, bucket in r['groups'].items():
        lines += ['', '## ' + kind.replace('_', ' ').title(), '']
        lines += table(bucket.items()) if bucket else ['No observations yet.']
    lines += ['', '## Top combinations: Session × Value area × CVD agreement', '',
              'Ranked by net P&L; small samples are not a strategy recommendation.', '']
    lines += table([(f"{s['session']} × {s['value_area_position']} × CVD {s['cvd_agrees']}", s)
                    for s in r['top_combinations'][:30]]) if r['top_combinations'] else ['No observations yet.']
    lines += ['', f"## Entry skips ({r['skips']['count']})", '', '| Reason | Count |', '|---|---:|']
    lines += [f'| {k} | {v} |' for k, v in sorted(r['skips']['by_reason'].items())] or ['| None yet | 0 |']
    lines += ['', 'Legacy records without telemetry remain in the totals and are grouped as unknown.',
              'MFE/MAE are gross price excursions, not hypothetical net realized P&L. Bar-data exit-bar extremes are ambiguous.', '']
    return '\n'.join(lines)


def write_report(logs_dir):
    root = Path(logs_dir)
    root.mkdir(parents=True, exist_ok=True)
    trades, te = read_records(root, 'lucid_trades.jsonl')
    events, ee = read_records(root, 'lucid_events.jsonl')
    r = build_report(trades, events)
    now = datetime.now(timezone.utc)
    r.update(generated_at_utc=now.isoformat(), generated_at_dubai=now.astimezone(ZoneInfo('Asia/Dubai')).strftime('%Y-%m-%d %H:%M:%S'),
             malformed_lines=te+ee)
    md = format_report(r)
    for filename, body in (('lucid_report.json', json.dumps(r, indent=2)), ('lucid_report.md', md)):
        p = root / filename
        tmp = p.with_suffix(p.suffix + '.tmp')
        tmp.write_text(body)
        os.replace(tmp, p)
    return r, md
