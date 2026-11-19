const express = require('express');
const http = require('http');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const app = express();
const PORT = process.env.PORT || 8001;
const TARGET_URL = process.env.TARGET_URL || 'http://127.0.0.1:8000';

const targetParsed = new URL(TARGET_URL);

const LOG_FILE = path.join(__dirname, 'proxy.log');

// Helper to log JSON lines to proxy.log
function logEvent(data) {
  const entry = {
    timestamp: new Date().toISOString(),
    ...data,
  };
  try {
    fs.appendFileSync(LOG_FILE, JSON.stringify(entry) + '\n', 'utf8');
  } catch (err) {
    console.error('Failed to append to proxy.log:', err);
  }
}

// Deterministic delay calculation from seed
function getSeedDelay(seed, defaultDelay = 100) {
  if (!seed) return defaultDelay;
  let hash = 0;
  const str = String(seed);
  for (let i = 0; i < str.length; i++) {
    hash = ((hash << 5) - hash + str.charCodeAt(i)) | 0;
  }
  const normalized = Math.abs(hash % 1000) / 1000;
  return Math.floor(50 + normalized * 150); // 50ms - 200ms
}

// Utility: health check
app.get('/health', (req, res) => {
  res.json({ status: 'ok', target: TARGET_URL, port: PORT });
});

// Utility: fetch log entries
app.get('/fault/logs', (req, res) => {
  if (!fs.existsSync(LOG_FILE)) {
    return res.json([]);
  }
  const lines = fs.readFileSync(LOG_FILE, 'utf8').trim().split('\n').filter(Boolean);
  const logs = lines.map(line => {
    try {
      return JSON.parse(line);
    } catch {
      return { raw: line };
    }
  });
  res.json(logs);
});

// Utility: reset log
app.post('/fault/reset-log', (req, res) => {
  fs.writeFileSync(LOG_FILE, '', 'utf8');
  res.json({ status: 'cleared' });
});

// Helper to forward HTTP request to target backend
function forwardToBackend({ method, path: reqPath, headers, body }, callback) {
  const options = {
    hostname: targetParsed.hostname,
    port: targetParsed.port || (targetParsed.protocol === 'https:' ? 443 : 80),
    path: reqPath,
    method: method,
    headers: headers,
  };

  const backendReq = http.request(options, (backendRes) => {
    const chunks = [];
    backendRes.on('data', chunk => chunks.push(chunk));
    backendRes.on('end', () => {
      const responseBody = Buffer.concat(chunks);
      callback(null, {
        statusCode: backendRes.statusCode,
        headers: backendRes.headers,
        body: responseBody,
      });
    });
  });

  backendReq.on('error', (err) => {
    callback(err);
  });

  if (body && body.length > 0) {
    backendReq.write(body);
  }
  backendReq.end();
}

// Catch-all proxy handler
app.use((req, res) => {
  const bodyChunks = [];
  req.on('data', chunk => bodyChunks.push(chunk));
  req.on('end', () => {
    const rawBody = Buffer.concat(bodyChunks);

    const rawFault = req.headers['x-fault'];
    const fault = (rawFault || 'none').toLowerCase().trim();
    const requestId = req.headers['x-request-id'] || `req_${Date.now()}_${Math.random().toString(36).substring(2, 8)}`;
    const seed = req.headers['x-seed'];
    const duplicateMode = (req.headers['x-duplicate-mode'] || 'delay').toLowerCase();
    const duplicateDelayHeader = req.headers['x-duplicate-delay'];

    // Clone headers and strip proxy control headers before forwarding
    const forwardedHeaders = { ...req.headers };
    delete forwardedHeaders['x-fault'];
    delete forwardedHeaders['x-seed'];
    delete forwardedHeaders['x-duplicate-mode'];
    delete forwardedHeaders['x-duplicate-delay'];
    forwardedHeaders['host'] = `${targetParsed.hostname}:${targetParsed.port || 80}`;
    forwardedHeaders['x-request-id'] = requestId;

    logEvent({
      requestId,
      fault,
      action: 'REQUEST_RECEIVED',
      method: req.method,
      url: req.originalUrl || req.url,
    });

    // 1. FAULT: drop_request (never forward, close connection)
    if (fault === 'drop_request') {
      logEvent({
        requestId,
        fault,
        action: 'DROP_REQUEST',
        detail: 'Request dropped before reaching server',
      });
      req.socket.destroy();
      return;
    }

    // 2. FAULT: drop_response (forward, let server finish, close connection without response)
    if (fault === 'drop_response') {
      logEvent({
        requestId,
        fault,
        action: 'FORWARD_REQUEST',
        detail: 'Forwarding to server; response will be dropped',
      });

      forwardToBackend(
        { method: req.method, path: req.originalUrl || req.url, headers: forwardedHeaders, body: rawBody },
        (err, backendRes) => {
          if (err) {
            logEvent({ requestId, fault, action: 'BACKEND_ERROR', error: err.message });
            req.socket.destroy();
            return;
          }
          logEvent({
            requestId,
            fault,
            action: 'DROP_RESPONSE',
            statusCode: backendRes.statusCode,
            detail: 'Server finished processing, response dropped before reaching client',
          });
          req.socket.destroy();
        }
      );
      return;
    }

    // 3. FAULT: crash (forward with X-Crash-Point: after_charge)
    if (fault === 'crash') {
      forwardedHeaders['x-crash-point'] = 'after_charge';
      logEvent({
        requestId,
        fault,
        action: 'INJECT_CRASH_HEADER',
        detail: 'Injected X-Crash-Point: after_charge',
      });

      forwardToBackend(
        { method: req.method, path: req.originalUrl || req.url, headers: forwardedHeaders, body: rawBody },
        (err, backendRes) => {
          if (err) {
            logEvent({ requestId, fault, action: 'BACKEND_ERROR', error: err.message });
            res.status(502).json({ error: 'Backend unreachable' });
            return;
          }
          logEvent({
            requestId,
            fault,
            action: 'RESPONSE_DELIVERED',
            statusCode: backendRes.statusCode,
          });
          res.writeHead(backendRes.statusCode, backendRes.headers);
          res.end(backendRes.body);
        }
      );
      return;
    }

    // 4. FAULT: duplicate (forward the same request twice: parallel or delayed)
    if (fault === 'duplicate') {
      const isParallel = duplicateMode === 'parallel';
      const delayMs = duplicateDelayHeader
        ? parseInt(duplicateDelayHeader, 10)
        : (isParallel ? 0 : getSeedDelay(seed, 100));

      logEvent({
        requestId,
        fault,
        action: 'FORWARD_DUPLICATE_INIT',
        mode: isParallel ? 'parallel' : 'delayed',
        delayMs,
      });

      // Send Request 1 (Primary)
      forwardToBackend(
        { method: req.method, path: req.originalUrl || req.url, headers: forwardedHeaders, body: rawBody },
        (err, backendRes) => {
          if (err) {
            logEvent({ requestId, fault, action: 'BACKEND_ERROR_PRIMARY', error: err.message });
            if (!res.headersSent) res.status(502).json({ error: 'Backend unreachable' });
            return;
          }
          logEvent({
            requestId,
            fault,
            action: 'RESPONSE_DELIVERED',
            statusCode: backendRes.statusCode,
            which: 'primary',
          });
          if (!res.headersSent) {
            res.writeHead(backendRes.statusCode, backendRes.headers);
            res.end(backendRes.body);
          }
        }
      );

      // Send Request 2 (Duplicate)
      const sendDuplicate = () => {
        const duplicateHeaders = { ...forwardedHeaders, 'x-request-duplicate': 'true' };
        logEvent({
          requestId,
          fault,
          action: 'FORWARD_DUPLICATE_SEND',
          delayMs,
        });
        forwardToBackend(
          { method: req.method, path: req.originalUrl || req.url, headers: duplicateHeaders, body: rawBody },
          (err, backendRes) => {
            if (err) {
              logEvent({ requestId, fault, action: 'BACKEND_ERROR_DUPLICATE', error: err.message });
              return;
            }
            logEvent({
              requestId,
              fault,
              action: 'DUPLICATE_RESPONSE_RECEIVED',
              statusCode: backendRes.statusCode,
            });
          }
        );
      };

      if (delayMs > 0) {
        setTimeout(sendDuplicate, delayMs);
      } else {
        sendDuplicate();
      }
      return;
    }

    // 5. FAULT: none (pass through)
    logEvent({
      requestId,
      fault: 'none',
      action: 'FORWARD_PASS',
      detail: 'Forwarding request normally',
    });

    forwardToBackend(
      { method: req.method, path: req.originalUrl || req.url, headers: forwardedHeaders, body: rawBody },
      (err, backendRes) => {
        if (err) {
          logEvent({ requestId, fault: 'none', action: 'BACKEND_ERROR', error: err.message });
          res.status(502).json({ error: 'Backend unreachable' });
          return;
        }
        logEvent({
          requestId,
          fault: 'none',
          action: 'RESPONSE_DELIVERED',
          statusCode: backendRes.statusCode,
        });
        res.writeHead(backendRes.statusCode, backendRes.headers);
        res.end(backendRes.body);
      }
    );
  });
});

if (require.main === module) {
  app.listen(PORT, () => {
    console.log(`Fault proxy running on port ${PORT}, forwarding to ${TARGET_URL}`);
  });
}

module.exports = { app, logEvent };
