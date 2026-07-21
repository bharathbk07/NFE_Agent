// NFE Agent — professional k6 HTML report (handleSummary).
// Tables: full TXN + full request + failed requests with URL/status.
// Metrics from custom nfe_* trends/counters tagged in the script.

function nfeEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function nfeNum(v, digits) {
  if (v == null || typeof v !== 'number' || isNaN(v)) return '—';
  if (digits == null) digits = 2;
  return Number(v).toFixed(digits);
}

function nfeMs(v) {
  if (v == null || typeof v !== 'number' || isNaN(v)) return '—';
  if (v < 1) return nfeNum(v, 3) + ' ms';
  if (v < 1000) return nfeNum(v, 2) + ' ms';
  return nfeNum(v / 1000, 2) + ' s';
}

function nfePct(rate) {
  if (rate == null || typeof rate !== 'number' || isNaN(rate)) return '—';
  return nfeNum(rate * 100, 2) + '%';
}

function nfeDurationHuman(ms) {
  if (ms == null || typeof ms !== 'number' || isNaN(ms)) return '—';
  if (ms < 1000) return nfeNum(ms / 1000, 2) + 's';
  var totalSec = Math.max(0, Math.round(ms / 1000));
  var mins = Math.floor(totalSec / 60);
  var secs = totalSec % 60;
  if (mins <= 0) return secs + 's';
  return mins + 'm ' + secs + 's';
}

function nfeParseTags(key) {
  var out = { base: key, tags: {} };
  var m = String(key).match(/^([^{]+)\{(.*)\}$/);
  if (!m) return out;
  out.base = m[1];
  var inner = m[2];
  // tag format: k:v,k2:v2 — values may contain ':' (URLs) so split on first ':' only per segment
  var parts = [];
  var buf = '';
  for (var i = 0; i < inner.length; i++) {
    var ch = inner.charAt(i);
    if (ch === ',') {
      parts.push(buf);
      buf = '';
    } else {
      buf += ch;
    }
  }
  if (buf) parts.push(buf);
  for (var j = 0; j < parts.length; j++) {
    var p = parts[j];
    var idx = p.indexOf(':');
    if (idx < 0) continue;
    out.tags[p.slice(0, idx)] = p.slice(idx + 1);
  }
  return out;
}

function nfeTrendVals(m) {
  var v = (m && m.values) || {};
  return {
    min: v.min,
    avg: v.avg,
    max: v.max,
    count: v.count,
    p50: v['p(50)'] != null ? v['p(50)'] : v.med,
    p90: v['p(90)'],
    p95: v['p(95)'],
    p99: v['p(99)'],
    med: v.med,
  };
}

function nfeCounterVals(m) {
  var v = (m && m.values) || {};
  return { count: v.count != null ? v.count : 0, rate: v.rate };
}

function nfeCollectByTags(data, baseName) {
  var rows = [];
  var metrics = data.metrics || {};
  var keys = Object.keys(metrics);
  for (var i = 0; i < keys.length; i++) {
    var key = keys[i];
    var parsed = nfeParseTags(key);
    if (parsed.base !== baseName) continue;
    if (!Object.keys(parsed.tags).length && key === baseName) {
      rows.push({ tags: {}, metric: metrics[key], key: key });
      continue;
    }
    if (!Object.keys(parsed.tags).length) continue;
    rows.push({ tags: parsed.tags, metric: metrics[key], key: key });
  }
  return rows;
}

function nfeThresholdRows(data) {
  var rows = [];
  var metrics = data.metrics || {};
  var names = Object.keys(metrics);
  for (var i = 0; i < names.length; i++) {
    var mname = names[i];
    var m = metrics[mname];
    if (!m || !m.thresholds) continue;
    var tnames = Object.keys(m.thresholds);
    for (var j = 0; j < tnames.length; j++) {
      var t = tnames[j];
      rows.push({
        metric: mname,
        threshold: t,
        ok: !!(m.thresholds[t] || {}).ok,
        values: m.values || {},
      });
    }
  }
  return rows;
}

function nfeBadge(ok) {
  return ok
    ? '<span class="badge pass">PASS</span>'
    : '<span class="badge fail">FAIL</span>';
}

function nfeBuildTxnTable(data) {
  var trends = nfeCollectByTags(data, 'nfe_txn_duration');
  var failByTxn = {};
  var countByTxn = {};
  var fails = nfeCollectByTags(data, 'nfe_req_fail');
  var counts = nfeCollectByTags(data, 'nfe_req_count');
  for (var i = 0; i < fails.length; i++) {
    var t = fails[i].tags.txn || '';
    failByTxn[t] = (failByTxn[t] || 0) + nfeCounterVals(fails[i].metric).count;
  }
  for (var j = 0; j < counts.length; j++) {
    var t2 = counts[j].tags.txn || '';
    countByTxn[t2] = (countByTxn[t2] || 0) + nfeCounterVals(counts[j].metric).count;
  }

  // Fallback: group_duration if custom metrics missing
  if (!trends.length) {
    var gd = nfeCollectByTags(data, 'group_duration');
    for (var g = 0; g < gd.length; g++) {
      var tags = gd[g].tags;
      var gname = tags.group || tags.txn || '';
      gname = String(gname).replace(/^::/, '');
      if (!gname) continue;
      trends.push({
        tags: { txn: gname },
        metric: gd[g].metric,
        key: gd[g].key,
      });
    }
  }

  var byTxn = {};
  for (var k = 0; k < trends.length; k++) {
    var txn = trends[k].tags.txn || 'unknown';
    if (!txn) continue;
    byTxn[txn] = trends[k];
  }
  var names = Object.keys(byTxn).sort();
  var html = '';
  for (var n = 0; n < names.length; n++) {
    var name = names[n];
    var tv = nfeTrendVals(byTxn[name].metric);
    var failed = failByTxn[name] || 0;
    var cnt = tv.count != null ? tv.count : countByTxn[name] || 0;
    html +=
      '<tr>' +
      '<td class="num">' +
      (n + 1) +
      '</td>' +
      '<td>' +
      nfeEsc(name) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.min) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.max) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.avg) +
      '</td>' +
      '<td class="num">' +
      nfeNum(cnt, 0) +
      '</td>' +
      '<td class="num' +
      (failed ? ' bad' : '') +
      '">' +
      nfeNum(failed, 0) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.p50) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.p90) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.p95) +
      '</td>' +
      '<td class="num">' +
      nfeMs(tv.p99) +
      '</td>' +
      '</tr>';
  }
  if (!html) {
    html =
      '<tr><td colspan="11" class="muted">No transaction metrics. Re-generate the k6 script so NFE helpers are included.</td></tr>';
  }
  return { html: html, count: names.length };
}

function nfeReqKey(tags) {
  return [tags.txn || '', tags.method || '', tags.url || tags.name || ''].join('|');
}

function nfeBuildReqTable(data) {
  var durations = nfeCollectByTags(data, 'nfe_req_duration');
  var counts = nfeCollectByTags(data, 'nfe_req_count');
  var fails = nfeCollectByTags(data, 'nfe_req_fail');

  var byKey = {};
  function ensure(tags) {
    var key = nfeReqKey(tags);
    if (!byKey[key]) {
      byKey[key] = {
        txn: tags.txn || '',
        method: tags.method || '',
        url: tags.url || tags.name || '',
        min: null,
        avg: null,
        max: null,
        count: 0,
        failed: 0,
      };
    }
    return byKey[key];
  }

  for (var i = 0; i < durations.length; i++) {
    var row = ensure(durations[i].tags);
    var tv = nfeTrendVals(durations[i].metric);
    row.min = tv.min;
    row.avg = tv.avg;
    row.max = tv.max;
    if (tv.count != null) row.count = tv.count;
  }
  for (var j = 0; j < counts.length; j++) {
    var row2 = ensure(counts[j].tags);
    var cv = nfeCounterVals(counts[j].metric).count;
    row2.count = Math.max(row2.count, cv);
  }
  for (var k = 0; k < fails.length; k++) {
    var row3 = ensure(fails[k].tags);
    row3.failed += nfeCounterVals(fails[k].metric).count;
  }

  // Merge status-split counters into URL rows (same txn/method/url, different status)
  var merged = {};
  var keys = Object.keys(byKey);
  for (var m = 0; m < keys.length; m++) {
    var r = byKey[keys[m]];
    var mk = [r.txn, r.method, r.url].join('|');
    if (!merged[mk]) {
      merged[mk] = {
        txn: r.txn,
        method: r.method,
        url: r.url,
        min: r.min,
        avg: r.avg,
        max: r.max,
        count: 0,
        failed: 0,
      };
    }
    var tgt = merged[mk];
    tgt.count += r.count || 0;
    tgt.failed += r.failed || 0;
    if (r.min != null && (tgt.min == null || r.min < tgt.min)) tgt.min = r.min;
    if (r.max != null && (tgt.max == null || r.max > tgt.max)) tgt.max = r.max;
    // weighted avg approximation when multiple status buckets
    if (r.avg != null && r.count) {
      if (tgt._wsum == null) {
        tgt._wsum = 0;
        tgt._wcnt = 0;
      }
      tgt._wsum += r.avg * r.count;
      tgt._wcnt += r.count;
      tgt.avg = tgt._wcnt ? tgt._wsum / tgt._wcnt : r.avg;
    } else if (tgt.avg == null) {
      tgt.avg = r.avg;
    }
  }

  var rows = Object.keys(merged)
    .map(function (k) {
      return merged[k];
    })
    .sort(function (a, b) {
      if (a.txn !== b.txn) return a.txn < b.txn ? -1 : 1;
      if (a.method !== b.method) return a.method < b.method ? -1 : 1;
      return a.url < b.url ? -1 : 1;
    });

  var html = '';
  for (var n = 0; n < rows.length; n++) {
    var x = rows[n];
    var failPct = x.count ? x.failed / x.count : 0;
    html +=
      '<tr>' +
      '<td class="num">' +
      (n + 1) +
      '</td>' +
      '<td>' +
      nfeEsc(x.txn) +
      '</td>' +
      '<td><code>' +
      nfeEsc(x.method) +
      '</code></td>' +
      '<td class="url">' +
      nfeEsc(x.url) +
      '</td>' +
      '<td class="num">' +
      nfeMs(x.min) +
      '</td>' +
      '<td class="num">' +
      nfeMs(x.avg) +
      '</td>' +
      '<td class="num">' +
      nfeMs(x.max) +
      '</td>' +
      '<td class="num">' +
      nfeNum(x.count, 0) +
      '</td>' +
      '<td class="num' +
      (x.failed ? ' bad' : '') +
      '">' +
      nfeNum(x.failed, 0) +
      '</td>' +
      '<td class="num' +
      (x.failed ? ' bad' : '') +
      '">' +
      nfePct(failPct) +
      '</td>' +
      '</tr>';
  }
  if (!html) {
    html =
      '<tr><td colspan="10" class="muted">No per-request metrics. Re-generate the k6 script.</td></tr>';
  }
  return { html: html, count: rows.length, rows: rows };
}

function nfeBuildFailedReqTable(data) {
  var fails = nfeCollectByTags(data, 'nfe_req_fail');
  var counts = nfeCollectByTags(data, 'nfe_req_count');
  var countMap = {};
  for (var i = 0; i < counts.length; i++) {
    countMap[nfeReqKey(counts[i].tags) + '|' + (counts[i].tags.status || '')] =
      nfeCounterVals(counts[i].metric).count;
  }

  var rows = [];
  for (var j = 0; j < fails.length; j++) {
    var t = fails[j].tags;
    var failed = nfeCounterVals(fails[j].metric).count;
    if (!failed) continue;
    var ck = nfeReqKey(t) + '|' + (t.status || '');
    var total = countMap[ck] || failed;
    rows.push({
      txn: t.txn || '',
      method: t.method || '',
      url: t.url || t.name || '',
      status: t.status || '—',
      failed: failed,
      total: total,
      failPct: total ? failed / total : 1,
    });
  }
  rows.sort(function (a, b) {
    return b.failed - a.failed;
  });

  var html = '';
  for (var n = 0; n < rows.length; n++) {
    var x = rows[n];
    html +=
      '<tr>' +
      '<td class="num">' +
      (n + 1) +
      '</td>' +
      '<td>' +
      nfeEsc(x.txn) +
      '</td>' +
      '<td><code>' +
      nfeEsc(x.method) +
      '</code></td>' +
      '<td class="url">' +
      nfeEsc(x.url) +
      '</td>' +
      '<td class="num bad">' +
      nfeEsc(x.status) +
      '</td>' +
      '<td class="num bad">' +
      nfeNum(x.failed, 0) +
      '</td>' +
      '<td class="num">' +
      nfeNum(x.total, 0) +
      '</td>' +
      '<td class="num bad">' +
      nfePct(x.failPct) +
      '</td>' +
      '</tr>';
  }
  if (!html) {
    html = '<tr><td colspan="8" class="muted">No failed requests.</td></tr>';
  }
  return { html: html, count: rows.length };
}

function nfeObs(data, txnCount, reqCount, failedCount) {
  var httpFail = ((data.metrics || {}).http_req_failed || {}).values || {};
  var dur = ((data.metrics || {}).http_req_duration || {}).values || {};
  var thr = nfeThresholdRows(data);
  var slaFail = 0;
  for (var i = 0; i < thr.length; i++) if (!thr[i].ok) slaFail++;
  var bullets = [];
  bullets.push(
    'Ran <strong>' +
      nfeEsc(nfeDurationHuman((data.state || {}).testRunDurationMs)) +
      '</strong> with <strong>' +
      txnCount +
      '</strong> transactions and <strong>' +
      reqCount +
      '</strong> distinct requests.'
  );
  bullets.push(
    'HTTP error rate: <strong>' +
      nfePct(httpFail.rate) +
      '</strong>; response time p95: <strong>' +
      nfeMs(dur['p(95)']) +
      '</strong>.'
  );
  if (failedCount) {
    bullets.push(
      '<strong>' +
        failedCount +
        '</strong> request/status bucket(s) recorded failures — see Failed request list (URL + status).'
    );
  } else {
    bullets.push('No failed requests recorded by NFE assertions/metrics.');
  }
  bullets.push(
    slaFail
      ? '<strong>' + slaFail + '</strong> SLA threshold(s) failed.'
      : 'All SLA thresholds passed.'
  );
  return bullets
    .map(function (b) {
      return '<li>' + b + '</li>';
    })
    .join('');
}

function nfeBuildHtmlReport(data) {
  var state = data.state || {};
  var opts = data.options || {};
  var thr = nfeThresholdRows(data);
  var slaOk = true;
  for (var i = 0; i < thr.length; i++) if (!thr[i].ok) slaOk = false;

  var txn = nfeBuildTxnTable(data);
  var req = nfeBuildReqTable(data);
  var failed = nfeBuildFailedReqTable(data);
  var overallOk = slaOk && failed.count === 0;

  var httpFail = ((data.metrics || {}).http_req_failed || {}).values || {};
  var dur = ((data.metrics || {}).http_req_duration || {}).values || {};
  var reqs = ((data.metrics || {}).http_reqs || {}).values || {};
  var iters = ((data.metrics || {}).iterations || {}).values || {};

  var slaRows = thr.length
    ? thr
        .map(function (r) {
          var actual = '';
          var v = r.values || {};
          if (v.rate != null) actual = 'rate=' + nfePct(v.rate);
          else if (v['p(95)'] != null)
            actual =
              'p95=' + nfeMs(v['p(95)']) + ', avg=' + nfeMs(v.avg) + ', max=' + nfeMs(v.max);
          else if (v.count != null) actual = 'count=' + nfeNum(v.count, 0);
          else actual = JSON.stringify(v);
          return (
            '<tr><td>' +
            nfeEsc(r.metric) +
            '</td><td><code>' +
            nfeEsc(r.threshold) +
            '</code></td><td>' +
            nfeEsc(actual) +
            '</td><td>' +
            nfeBadge(r.ok) +
            '</td></tr>'
          );
        })
        .join('')
    : '<tr><td colspan="4" class="muted">No thresholds defined.</td></tr>';

  var scenario = '';
  try {
    scenario = nfeEsc(JSON.stringify(opts.scenarios || opts, null, 2));
  } catch (e) {
    scenario = '—';
  }

  return (
    '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"/>' +
    '<meta name="viewport" content="width=device-width,initial-scale=1"/>' +
    '<title>NFE k6 Test Report</title><style>' +
    ':root{--ink:#111827;--muted:#6b7280;--line:#e5e7eb;--bg:#f3f4f6;--card:#fff;--pass:#166534;--fail:#991b1b;--head:#0f766e}' +
    '*{box-sizing:border-box}body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;background:var(--bg);color:var(--ink);line-height:1.45}' +
    'header{background:linear-gradient(120deg,#0f766e,#115e59 55%,#134e4a);color:#ecfdf5;padding:28px 20px}' +
    'header h1{margin:0 0 6px;font-size:1.6rem}header p{margin:0;opacity:.9}' +
    '.wrap{max-width:1280px;margin:0 auto;padding:18px 14px 40px}' +
    '.pill{display:inline-block;margin-top:12px;padding:6px 12px;border-radius:999px;font-weight:700;font-size:.85rem}' +
    '.pill.ok{background:#bbf7d0;color:#14532d}.pill.bad{background:#fecaca;color:#7f1d1d}' +
    '.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:10px;margin:14px 0}' +
    '.kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}.kpi .l{font-size:.72rem;text-transform:uppercase;color:var(--muted);letter-spacing:.04em}.kpi .v{font-size:1.2rem;font-weight:700;margin-top:4px}' +
    'section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:14px 0;box-shadow:0 1px 2px rgba(0,0,0,.03)}' +
    'h2{margin:0 0 10px;font-size:1.05rem;padding-bottom:8px;border-bottom:1px solid var(--line)}' +
    'p.note{margin:0 0 10px;color:var(--muted);font-size:.9rem}' +
    '.scroll{overflow:auto;border:1px solid var(--line);border-radius:8px}' +
    'table{width:100%;border-collapse:collapse;font-size:.86rem;min-width:720px}' +
    'th,td{padding:8px 10px;border-bottom:1px solid var(--line);text-align:left;vertical-align:top}' +
    'th{background:#f8fafc;position:sticky;top:0;font-size:.72rem;text-transform:uppercase;letter-spacing:.03em;color:var(--muted);white-space:nowrap}' +
    'td.num,.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}' +
    'td.url{max-width:420px;word-break:break-all;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.8rem}' +
    'td.bad,.bad{color:var(--fail);font-weight:600}' +
    '.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:.72rem;font-weight:700}' +
    '.badge.pass{background:#dcfce7;color:var(--pass)}.badge.fail{background:#fee2e2;color:var(--fail)}' +
    'ul{margin:0;padding-left:1.2rem}li{margin:6px 0}.muted{color:var(--muted)}code{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:.8rem}' +
    'pre{background:#0b1220;color:#e5e7eb;padding:12px;border-radius:8px;overflow:auto;font-size:.75rem}' +
    'footer{text-align:center;color:var(--muted);font-size:.8rem;margin-top:18px}' +
    '</style></head><body><header><div class="wrap" style="padding:0">' +
    '<h1>k6 Performance Test Report</h1>' +
    '<p>NFE Agent · transaction &amp; request detail · assertions · SLA</p>' +
    '<div class="pill ' +
    (overallOk ? 'ok' : 'bad') +
    '">Overall: ' +
    (overallOk ? 'PASS' : 'FAIL') +
    '</div></div></header><div class="wrap">' +
    '<div class="kpis">' +
    '<div class="kpi"><div class="l">Duration</div><div class="v">' +
    nfeEsc(nfeDurationHuman(state.testRunDurationMs)) +
    '</div></div>' +
    '<div class="kpi"><div class="l">HTTP reqs</div><div class="v">' +
    nfeNum(reqs.count, 0) +
    '</div></div>' +
    '<div class="kpi"><div class="l">Iterations</div><div class="v">' +
    nfeNum(iters.count, 0) +
    '</div></div>' +
    '<div class="kpi"><div class="l">Error rate</div><div class="v">' +
    nfePct(httpFail.rate) +
    '</div></div>' +
    '<div class="kpi"><div class="l">p95 latency</div><div class="v">' +
    nfeMs(dur['p(95)']) +
    '</div></div>' +
    '<div class="kpi"><div class="l">Failed req buckets</div><div class="v">' +
    failed.count +
    '</div></div>' +
    '</div>' +
    '<section><h2>1. General test details</h2>' +
    '<table><tbody>' +
    '<tr><th>Generated</th><td>' +
    nfeEsc(new Date().toISOString()) +
    '</td></tr>' +
    '<tr><th>Test duration</th><td>' +
    nfeEsc(nfeDurationHuman(state.testRunDurationMs)) +
    ' (' +
    nfeNum(state.testRunDurationMs, 0) +
    ' ms)</td></tr>' +
    '<tr><th>Scenarios</th><td><pre>' +
    scenario +
    '</pre></td></tr>' +
    '</tbody></table></section>' +
    '<section><h2>2. Test observation</h2><ul>' +
    nfeObs(data, txn.count, req.count, failed.count) +
    '</ul></section>' +
    '<section><h2>3. Full transaction table</h2>' +
    '<p class="note">Si.No · TXN · min / max / avg · count · failed count · p50 / p90 / p95 / p99</p>' +
    '<div class="scroll"><table><thead><tr>' +
    '<th class="num">Si.No</th><th>TXN name</th><th class="num">Min</th><th class="num">Max</th><th class="num">Avg</th>' +
    '<th class="num">Count</th><th class="num">Failed count</th>' +
    '<th class="num">Perc 50</th><th class="num">Perc 90</th><th class="num">Perc 95</th><th class="num">Perc 99</th>' +
    '</tr></thead><tbody>' +
    txn.html +
    '</tbody></table></div></section>' +
    '<section><h2>4. Full request table</h2>' +
    '<p class="note">Si.No · TXN · method · URL · min / avg / max · count · failed count · failed %</p>' +
    '<div class="scroll"><table><thead><tr>' +
    '<th class="num">Si.No</th><th>TXN</th><th>Method</th><th>URL</th>' +
    '<th class="num">Min</th><th class="num">Avg</th><th class="num">Max</th>' +
    '<th class="num">Count</th><th class="num">Failed count</th><th class="num">Failed %</th>' +
    '</tr></thead><tbody>' +
    req.html +
    '</tbody></table></div></section>' +
    '<section><h2>5. Failed request list</h2>' +
    '<p class="note">Includes URL and HTTP status returned by the server (or 0 when the request did not complete).</p>' +
    '<div class="scroll"><table><thead><tr>' +
    '<th class="num">Si.No</th><th>TXN</th><th>Method</th><th>URL</th><th class="num">Status</th>' +
    '<th class="num">Failed</th><th class="num">Total</th><th class="num">Failed %</th>' +
    '</tr></thead><tbody>' +
    failed.html +
    '</tbody></table></div></section>' +
    '<section><h2>6. SLA details (thresholds)</h2>' +
    '<div class="scroll"><table><thead><tr><th>Metric</th><th>Threshold</th><th>Observed</th><th>Result</th></tr></thead><tbody>' +
    slaRows +
    '</tbody></table></div></section>' +
    '<footer>Generated by NFE Agent</footer></div></body></html>'
  );
}

function nfeTextSummary(data) {
  var failed = nfeBuildFailedReqTable(data);
  var thr = nfeThresholdRows(data);
  var failThr = 0;
  for (var i = 0; i < thr.length; i++) if (!thr[i].ok) failThr++;
  return (
    'NFE k6 summary\n' +
    '  duration: ' +
    nfeDurationHuman((data.state || {}).testRunDurationMs) +
    '\n' +
    '  failed request buckets: ' +
    failed.count +
    '\n' +
    '  thresholds failed: ' +
    failThr +
    '/' +
    thr.length +
    '\n' +
    '  html report: ' +
    (__ENV.NFE_K6_HTML_REPORT || 'html-report.html') +
    '\n'
  );
}

export function handleSummary(data) {
  var htmlPath = __ENV.NFE_K6_HTML_REPORT || 'html-report.html';
  var jsonPath = __ENV.NFE_K6_SUMMARY_JSON || '';
  var out = {};
  var html = nfeBuildHtmlReport(data);
  out[htmlPath] = html;
  // Also write canonical name beside artifacts when a stem-specific path is used
  if (htmlPath.indexOf('html-report.html') < 0) {
    try {
      var dir = htmlPath.replace(/[^\/\\]+$/, '');
      out[dir + 'html-report.html'] = html;
    } catch (e) {}
  }
  if (jsonPath) out[jsonPath] = JSON.stringify(data);
  out.stdout = nfeTextSummary(data);
  return out;
}
