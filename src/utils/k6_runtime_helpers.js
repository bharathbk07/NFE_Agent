// NFE runtime helpers — custom metrics + response assertions
const nfeTxnDuration = new Trend('nfe_txn_duration', true);
const nfeReqDuration = new Trend('nfe_req_duration', true);
const nfeReqCount = new Counter('nfe_req_count');
const nfeReqFail = new Counter('nfe_req_fail');

function nfeShortUrl(u) {
  var s = String(u == null ? '' : u);
  if (s.length > 200) return s.slice(0, 197) + '...';
  return s;
}

function nfeReqTags(txn, method, url, status) {
  var shortUrl = nfeShortUrl(url);
  return {
    txn: String(txn || ''),
    method: String(method || ''),
    url: shortUrl,
    name: String(method || '') + ' ' + shortUrl,
    status: String(status == null ? '' : status),
  };
}

function nfeAssertResponse(res, txn, method, opts) {
  opts = opts || {};
  var soft = !!opts.soft;
  var expectJson = !!opts.expectJson;
  var url = (res && res.url) || opts.label || '';
  var status = res ? res.status : 0;
  var tags = nfeReqTags(txn, method, url, status);
  var dur = res && res.timings ? res.timings.duration : 0;

  nfeReqDuration.add(dur, tags);
  nfeReqCount.add(1, tags);
  if (!res || status === 0 || status >= 400) {
    nfeReqFail.add(1, tags);
  }

  var checks = {};
  if (soft) {
    checks[txn + ' ' + method + ' status <500'] = function (r) {
      return r && r.status > 0 && r.status < 500;
    };
  } else {
    checks[txn + ' ' + method + ' status is 2xx'] = function (r) {
      return r && r.status >= 200 && r.status < 300;
    };
  }
  checks[txn + ' ' + method + ' has body'] = function (r) {
    return r && r.body !== null && r.body !== undefined && String(r.body).length > 0;
  };
  checks[txn + ' ' + method + ' duration recorded'] = function (r) {
    return r && r.timings && r.timings.duration >= 0;
  };
  if (expectJson) {
    checks[txn + ' ' + method + ' body is JSON'] = function (r) {
      if (!r || !r.body) return false;
      try {
        r.json();
        return true;
      } catch (e) {
        return false;
      }
    };
  }
  return check(res, checks);
}

function nfeMarkTxn(txn, startedAtMs) {
  var elapsed = Date.now() - startedAtMs;
  if (elapsed < 0) elapsed = 0;
  nfeTxnDuration.add(elapsed, { txn: String(txn || '') });
}
