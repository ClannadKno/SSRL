/**
 * Collaborative Editor API service.
 * Wraps fetch calls to the collaborative-documents API.
 *
 * Batch 9: Added fetchWithTimeout, RequestTimeoutError, requestTicketWithRetry.
 */

const CSRF_HEADERS = {
  "Content-Type": "application/json",
};

// ---------------------------------------------------------------------------
// RequestTimeoutError
// ---------------------------------------------------------------------------

/**
 * Error thrown when a fetch request times out.
 */
export class RequestTimeoutError extends Error {
  constructor(url, timeoutMs) {
    super("Request timed out after " + timeoutMs + "ms: " + url);
    this.name = "RequestTimeoutError";
    this.url = url;
    this.timeoutMs = timeoutMs;
  }
}

// ---------------------------------------------------------------------------
// fetchWithTimeout
// ---------------------------------------------------------------------------

/**
 * Fetch with a timeout via AbortController.
 * @param {string} url
 * @param {object} options - Standard fetch options (credentials, headers, etc.)
 * @param {number} timeoutMs - Timeout in milliseconds (default 10000)
 * @returns {Promise<Response>}
 * @throws {RequestTimeoutError} on timeout
 */
export async function fetchWithTimeout(url, options, timeoutMs) {
  if (timeoutMs == null) timeoutMs = 10000;
  var controller = new AbortController();
  var timeoutId = setTimeout(function () { controller.abort(); }, timeoutMs);
  try {
    var response = await fetch(url, Object.assign({}, options, { signal: controller.signal }));
    return response;
  } catch (err) {
    if (err.name === "AbortError") {
      throw new RequestTimeoutError(url, timeoutMs);
    }
    throw err;
  } finally {
    clearTimeout(timeoutId);
  }
}

// ---------------------------------------------------------------------------
// requestTicketWithRetry
// ---------------------------------------------------------------------------

var RETRYABLE_STATUSES = new Set([408, 429, 500, 502, 503, 504]);

function _isRetryableError(err) {
  if (err instanceof RequestTimeoutError) return true;
  // Network errors (TypeError from fetch) are retryable
  if (err.name === "TypeError") return true;
  return false;
}

function _delay(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
}

/**
 * Request a collaboration ticket with limited retry.
 *
 * Retry policy:
 * - 401, 403, 404: no retry, throw immediately with descriptive error.
 * - 408, 429, 500, 502, 503, 504: retry up to maxRetries times.
 * - Network errors / timeouts: retry up to maxRetries times.
 * - Retry backoff: (attempt+1) * 300 + random jitter 0-200ms.
 *
 * @param {string} apiBase
 * @param {string|number} documentId
 * @param {string} permission - "edit" or "view"
 * @param {string} tabToken
 * @param {number} maxRetries - default 3
 * @returns {Promise<object>} ticket data
 * @throws {Error} with .code, .status, .retries properties
 */
export async function requestTicketWithRetry(apiBase, documentId, permission, tabToken, maxRetries) {
  if (maxRetries == null) maxRetries = 3;
  var endpoint = permission === "view"
    ? apiBase + "/" + documentId + "/teacher-ticket"
    : apiBase + "/" + documentId + "/ticket";

  var headers = {};
  if (tabToken) headers["X-Tab-Token"] = tabToken;

  var lastError = null;

  for (var attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      var response = await fetchWithTimeout(endpoint, {
        method: "POST",
        headers: headers,
      }, 10000);

      if (response.ok) {
        return await response.json();
      }

      // Non-retryable status codes - throw immediately
      if (response.status === 401) {
        var err = new Error("\u8ba4\u8bc1\u5931\u8d25\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55");
        err.code = "AUTH_FAILED";
        err.status = 401;
        throw err;
      }
      if (response.status === 403) {
        var err = new Error("\u6743\u9650\u9a8c\u8bc1\u672a\u901a\u8fc7\uff0c\u8bf7\u91cd\u65b0\u767b\u5f55\u540e\u518d\u8bd5");
        err.code = "PERMISSION_DENIED";
        err.status = 403;
        throw err;
      }
      if (response.status === 404) {
        var err = new Error("\u6587\u6863\u4e0d\u5b58\u5728");
        err.code = "NOT_FOUND";
        err.status = 404;
        throw err;
      }

      // Retryable status codes
      if (RETRYABLE_STATUSES.has(response.status)) {
        lastError = new Error("\u670d\u52a1\u5668\u54cd\u5e94\u5f02\u5e38 (HTTP " + response.status + ")");
        lastError.status = response.status;
      } else {
        // Other non-retryable status
        var err = new Error("\u8bf7\u6c42\u5931\u8d25 (HTTP " + response.status + ")");
        err.status = response.status;
        throw err;
      }
    } catch (err) {
      if (err.code === "AUTH_FAILED" || err.code === "PERMISSION_DENIED" || err.code === "NOT_FOUND") {
        throw err; // Non-retryable, throw immediately
      }
      if (_isRetryableError(err)) {
        lastError = err;
      } else {
        throw err; // Unknown error, throw immediately
      }
    }

    // Retry with backoff + jitter
    if (attempt < maxRetries) {
      var delayMs = (attempt + 1) * 300 + Math.random() * 200;
      await _delay(delayMs);
    }
  }

  // All retries exhausted
  var finalError = lastError || new Error("\u8fde\u63a5\u5931\u8d25\uff0c\u8bf7\u7a0d\u540e\u5237\u65b0\u9875\u9762");
  finalError.code = "TICKET_FAILED";
  finalError.retries = maxRetries;
  throw finalError;
}

export async function fetchState(apiBase, documentId) {
  const res = await fetch(apiBase + "/" + documentId + "/state");
  if (!res.ok) throw new Error("Failed to load state: HTTP " + res.status);
  return res.json();
}

export async function saveSnapshot(apiBase, documentId, payload) {
  const res = await fetch(apiBase + "/" + documentId + "/snapshot", {
    method: "POST",
    headers: CSRF_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || "HTTP " + res.status);
  }
  return res.json();
}

export async function submitDocument(apiBase, documentId, payload) {
  const res = await fetch(apiBase + "/" + documentId + "/submit", {
    method: "POST",
    headers: CSRF_HEADERS,
    body: JSON.stringify(payload),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.error || "HTTP " + res.status);
  }
  return res.json();
}

export async function fetchCurrentDocument(apiBase) {
  const res = await fetch(apiBase + "/current");
  if (!res.ok) throw new Error("Failed to get current document: HTTP " + res.status);
  return res.json();
}
