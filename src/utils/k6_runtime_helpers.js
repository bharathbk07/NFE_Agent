// NFE runtime helpers — custom metrics + response assertions
const nfeTxnDuration = new Trend('nfe_txn_duration', true);
const nfeTxnFail = new Counter('nfe_txn_fail');
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

/**
 * Record request metrics + checks.
 * Returns true when the request is considered failed (for TXN roll-up).
 * Soft checks still count HTTP >=400 as a request fail for reporting, but
 * do not fail k6 checks on 4xx.
 */
function nfeAssertResponse(res, txn, method, opts) {
  opts = opts || {};
  var soft = !!opts.soft;
  var expectJson = !!opts.expectJson;
  var requireAuthSession = !!opts.requireAuthSession;
  var url = (res && res.url) || opts.label || '';
  var status = res ? res.status : 0;
  var tags = nfeReqTags(txn, method, url, status);
  var dur = res && res.timings ? res.timings.duration : 0;
  // Script bug = network error (0) or 4xx. Application 5xx is tolerated.
  var reqFailed = !res || status === 0 || (status >= 400 && status < 500);

  nfeReqDuration.add(dur, tags);
  nfeReqCount.add(1, tags);
  if (reqFailed) {
    nfeReqFail.add(1, tags);
  }

  var checks = {};
  if (soft) {
    checks[txn + ' ' + method + ' not 4xx'] = function (r) {
      return r && r.status > 0 && !(r.status >= 400 && r.status < 500);
    };
  } else {
    checks[txn + ' ' + method + ' ok (2xx) or app 5xx'] = function (r) {
      if (!r || !r.status) return false;
      if (r.status >= 200 && r.status < 400) return true;
      // Application fault — not a scripting/correlation error
      if (r.status >= 500 && r.status < 600) return true;
      return false;
    };
  }
  checks[txn + ' ' + method + ' has body'] = function (r) {
    // 5xx may return empty bodies; don't fail the script for app outages
    if (r && r.status >= 500 && r.status < 600) return true;
    return r && r.body !== null && r.body !== undefined && String(r.body).length > 0;
  };
  checks[txn + ' ' + method + ' duration recorded'] = function (r) {
    return r && r.timings && r.timings.duration >= 0;
  };
  if (expectJson) {
    checks[txn + ' ' + method + ' body is JSON'] = function (r) {
      if (!r) return false;
      if (r.status >= 500 && r.status < 600) return true;
      if (!r.body) return false;
      try {
        r.json();
        return true;
      } catch (e) {
        return false;
      }
    };
  }
  if (requireAuthSession) {
    checks[txn + ' ' + method + ' session established'] = function (r) {
      if (!r) return false;
      var u = String(r.url || '');
      if (/\/auth\/login\b/i.test(u)) return false;
      var body = String(r.body || '');
      // Still showing the login form ⇒ CSRF/session failed silently (HTTP 200).
      if (/name=["']username["']/i.test(body) && /name=["']password["']/i.test(body)) {
        return false;
      }
      return r.status >= 200 && r.status < 400;
    };
  }
  check(res, checks);
  if (requireAuthSession && res && !reqFailed) {
    var finalUrl = String(res.url || '');
    var body = String(res.body || '');
    var stillOnLogin =
      /\/auth\/login\b/i.test(finalUrl) ||
      (/name=["']username["']/i.test(body) && /name=["']password["']/i.test(body));
    if (stillOnLogin) {
      reqFailed = true;
      nfeReqFail.add(1, nfeReqTags(txn, method, url, String(status || '200')));
    }
  }
  return reqFailed;
}

/**
 * Close a TXN sample. ``failed`` must be true when any request in this
 * iteration failed — so TXN failed count never exceeds TXN count.
 */
function nfeMarkTxn(txn, startedAtMs, failed) {
  var elapsed = Date.now() - startedAtMs;
  if (elapsed < 0) elapsed = 0;
  var tags = { txn: String(txn || '') };
  nfeTxnDuration.add(elapsed, tags);
  if (failed) {
    nfeTxnFail.add(1, tags);
  }
}
