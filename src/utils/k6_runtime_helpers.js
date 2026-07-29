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

/** Resolve ``$.a.0.b`` / ``a.b`` against a parsed JSON value. */
function nfeJsonPathExists(obj, path) {
  var p = String(path || '').replace(/^\$\.?/, '');
  if (!p) return obj !== undefined && obj !== null;
  var parts = p.split('.');
  var cur = obj;
  for (var i = 0; i < parts.length; i++) {
    if (cur === undefined || cur === null) return false;
    var key = parts[i];
    if (Array.isArray(cur)) {
      var idx = parseInt(key, 10);
      if (isNaN(idx) || idx < 0 || idx >= cur.length) return false;
      cur = cur[idx];
      continue;
    }
    if (typeof cur !== 'object') return false;
    if (!(key in cur)) return false;
    cur = cur[key];
  }
  return cur !== undefined && cur !== null;
}

/**
 * Evaluate IR content assertion; returns true when content checks fail.
 * Also registers k6 check() entries via the shared ``checks`` object.
 */
function nfeApplyContentAssertion(res, txn, method, assertion, checks) {
  if (!assertion) return false;
  var failed = false;
  var bodyStr = res ? String(res.body || '') : '';
  var status = res ? res.status : 0;

  if (assertion.expect_status && assertion.expect_status.length) {
    checks[txn + ' ' + method + ' expect status'] = function (r) {
      if (!r || !r.status) return false;
      if (r.status >= 500 && r.status < 600) return true;
      for (var i = 0; i < assertion.expect_status.length; i++) {
        if (r.status === assertion.expect_status[i]) return true;
      }
      return false;
    };
    var statusOk = false;
    if (status >= 500 && status < 600) statusOk = true;
    else {
      for (var si = 0; si < assertion.expect_status.length; si++) {
        if (status === assertion.expect_status[si]) {
          statusOk = true;
          break;
        }
      }
    }
    if (!statusOk) failed = true;
  }

  if (assertion.body_contains && assertion.body_contains.length) {
    for (var ci = 0; ci < assertion.body_contains.length; ci++) {
      (function (needle, idx) {
        var short = needle.length > 24 ? needle.slice(0, 21) + '...' : needle;
        var label = txn + ' ' + method + ' body contains [' + idx + '] ' + short;
        checks[label] = function (r) {
          if (!r) return false;
          if (r.status >= 500 && r.status < 600) return true;
          return String(r.body || '').indexOf(needle) >= 0;
        };
        if (!(status >= 500 && status < 600) && bodyStr.indexOf(needle) < 0) {
          failed = true;
        }
      })(String(assertion.body_contains[ci]), ci);
    }
  }

  if (assertion.body_not_contains && assertion.body_not_contains.length) {
    for (var ni = 0; ni < assertion.body_not_contains.length; ni++) {
      (function (needle, idx) {
        var short = needle.length > 24 ? needle.slice(0, 21) + '...' : needle;
        var label = txn + ' ' + method + ' body not contains [' + idx + '] ' + short;
        checks[label] = function (r) {
          if (!r) return false;
          if (r.status >= 500 && r.status < 600) return true;
          return String(r.body || '').indexOf(needle) < 0;
        };
        if (!(status >= 500 && status < 600) && bodyStr.indexOf(needle) >= 0) {
          failed = true;
        }
      })(String(assertion.body_not_contains[ni]), ni);
    }
  }

  if (assertion.json_path_exists && assertion.json_path_exists.length) {
    var parsed = null;
    var parseOk = false;
    try {
      if (res && res.body) {
        parsed = typeof res.json === 'function' ? res.json() : JSON.parse(bodyStr);
        parseOk = true;
      }
    } catch (e) {
      parseOk = false;
    }
    for (var ji = 0; ji < assertion.json_path_exists.length; ji++) {
      (function (jp) {
        var label = txn + ' ' + method + ' json path ' + jp;
        checks[label] = function (r) {
          if (!r) return false;
          if (r.status >= 500 && r.status < 600) return true;
          try {
            var data = typeof r.json === 'function' ? r.json() : JSON.parse(String(r.body || ''));
            return nfeJsonPathExists(data, jp);
          } catch (e2) {
            return false;
          }
        };
        if (!(status >= 500 && status < 600)) {
          if (!parseOk || !nfeJsonPathExists(parsed, jp)) failed = true;
        }
      })(String(assertion.json_path_exists[ji]));
    }
  }

  return failed;
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

  var contentFailed = nfeApplyContentAssertion(
    res,
    txn,
    method,
    opts.assertion,
    checks
  );

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
  if (contentFailed && !reqFailed) {
    reqFailed = true;
    nfeReqFail.add(1, nfeReqTags(txn, method, url, String(status || '')));
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
