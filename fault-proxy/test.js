/**
 * Self-contained test suite for fault-proxy.
 * Runs a mock backend on port 9002 and proxy on port 9001.
 */

const http = require('http');
const fs = require('fs');
const path = require('path');
const assert = require('assert');

process.env.PORT = '9001';
process.env.TARGET_URL = 'http://localhost:9002';

const { app } = require('./index');
const LOG_FILE = path.join(__dirname, 'proxy.log');

// Clear log file before tests
if (fs.existsSync(LOG_FILE)) {
  fs.writeFileSync(LOG_FILE, '', 'utf8');
}

let mockBackendRequests = [];
let mockBackendServer;
let proxyServer;

function startMockBackend(port) {
  return new Promise((resolve) => {
    mockBackendServer = http.createServer((req, res) => {
      const bodyChunks = [];
      req.on('data', chunk => bodyChunks.push(chunk));
      req.on('end', () => {
        const body = Buffer.concat(bodyChunks).toString();
        mockBackendRequests.push({
          method: req.method,
          url: req.url,
          headers: req.headers,
          body,
        });

        if (req.headers['x-crash-point'] === 'after_charge') {
          res.writeHead(500, { 'Content-Type': 'application/json' });
          res.end(JSON.stringify({ error: 'Simulated crash after charge' }));
          return;
        }

        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ success: true, count: mockBackendRequests.length }));
      });
    });

    mockBackendServer.listen(port, () => resolve());
  });
}

function startProxyServer(port) {
  return new Promise((resolve) => {
    proxyServer = app.listen(port, () => resolve());
  });
}

function makeClientRequest({ method = 'POST', path = '/book', headers = {}, body = '{}' }) {
  return new Promise((resolve, reject) => {
    const req = http.request({
      hostname: 'localhost',
      port: 9001,
      path,
      method,
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(body),
        ...headers,
      },
    }, (res) => {
      const chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        resolve({
          statusCode: res.statusCode,
          headers: res.headers,
          body: Buffer.concat(chunks).toString(),
        });
      });
    });

    req.on('error', (err) => {
      resolve({ error: err.code || err.message });
    });

    req.write(body);
    req.end();
  });
}

async function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function runTests() {
  console.log('--- Starting Fault Proxy Tests ---');
  await startMockBackend(9002);
  await startProxyServer(9001);

  try {
    // 1. Test "none" (pass through)
    console.log('1. Testing X-Fault: none (pass through)...');
    mockBackendRequests = [];
    const resNone = await makeClientRequest({
      headers: { 'X-Fault': 'none', 'X-Request-Id': 'req-test-none' },
      body: JSON.stringify({ user_id: 'u1', slot_id: 's1', amount: 100 }),
    });
    assert.strictEqual(resNone.statusCode, 200, 'Expected status 200 for none');
    assert.strictEqual(mockBackendRequests.length, 1, 'Backend should receive exactly 1 request');
    assert.strictEqual(mockBackendRequests[0].headers['x-fault'], undefined, 'X-Fault header must be stripped');
    console.log('   ✓ Pass-through forwarded correctly and X-Fault was stripped');

    // 2. Test "drop_request"
    console.log('2. Testing X-Fault: drop_request...');
    mockBackendRequests = [];
    const resDropReq = await makeClientRequest({
      headers: { 'X-Fault': 'drop_request', 'X-Request-Id': 'req-test-drop-req' },
      body: JSON.stringify({ user_id: 'u1', slot_id: 's1', amount: 100 }),
    });
    assert.ok(resDropReq.error, 'Expected connection error when request dropped');
    assert.strictEqual(mockBackendRequests.length, 0, 'Backend must never receive dropped request');
    console.log('   ✓ Request was dropped before forwarding, connection closed');

    // 3. Test "drop_response"
    console.log('3. Testing X-Fault: drop_response...');
    mockBackendRequests = [];
    const resDropRes = await makeClientRequest({
      headers: { 'X-Fault': 'drop_response', 'X-Request-Id': 'req-test-drop-res' },
      body: JSON.stringify({ user_id: 'u1', slot_id: 's1', amount: 100 }),
    });
    assert.ok(resDropRes.error, 'Expected client connection error when response is dropped');
    assert.strictEqual(mockBackendRequests.length, 1, 'Backend must have received and processed request');
    console.log('   ✓ Backend processed request, but client connection was closed without response');

    // 4. Test "crash"
    console.log('4. Testing X-Fault: crash...');
    mockBackendRequests = [];
    const resCrash = await makeClientRequest({
      headers: { 'X-Fault': 'crash', 'X-Request-Id': 'req-test-crash' },
      body: JSON.stringify({ user_id: 'u1', slot_id: 's1', amount: 100 }),
    });
    assert.strictEqual(resCrash.statusCode, 500, 'Expected status 500 from crash simulation');
    assert.strictEqual(mockBackendRequests.length, 1);
    assert.strictEqual(
      mockBackendRequests[0].headers['x-crash-point'],
      'after_charge',
      'Backend must receive X-Crash-Point: after_charge header'
    );
    console.log('   ✓ X-Crash-Point: after_charge injected and 500 status returned');

    // 5. Test "duplicate"
    console.log('5. Testing X-Fault: duplicate...');
    mockBackendRequests = [];
    const resDuplicate = await makeClientRequest({
      headers: {
        'X-Fault': 'duplicate',
        'X-Duplicate-Mode': 'delay',
        'X-Duplicate-Delay': '50',
        'X-Request-Id': 'req-test-duplicate',
      },
      body: JSON.stringify({ user_id: 'u1', slot_id: 's1', amount: 100 }),
    });
    assert.strictEqual(resDuplicate.statusCode, 200, 'Client receives 200 for primary request');
    // Allow short delay for duplicate request to arrive at backend
    await sleep(120);
    assert.strictEqual(mockBackendRequests.length, 2, 'Backend must receive exactly 2 requests');
    console.log('   ✓ Single client request forwarded twice to backend (duplicate delivery)');

    // 6. Test JSON lines in proxy.log
    console.log('6. Validating proxy.log entries...');
    assert.ok(fs.existsSync(LOG_FILE), 'proxy.log must exist');
    const logContent = fs.readFileSync(LOG_FILE, 'utf8').trim();
    const logLines = logContent.split('\n').filter(Boolean);
    assert.ok(logLines.length >= 6, 'proxy.log should have recorded events for all tests');

    for (const line of logLines) {
      const entry = JSON.parse(line);
      assert.ok(entry.timestamp, 'Log entry must have timestamp');
      assert.ok(entry.requestId, 'Log entry must have requestId');
      assert.ok(entry.fault, 'Log entry must have fault');
      assert.ok(entry.action, 'Log entry must have action');
    }
    console.log(`   ✓ proxy.log successfully recorded ${logLines.length} valid JSON lines`);

    console.log('\nAll 6 fault proxy test suites passed successfully!');
  } finally {
    if (mockBackendServer) mockBackendServer.close();
    if (proxyServer) proxyServer.close();
  }
}

if (require.main === module) {
  runTests().catch((err) => {
    console.error('Test suite failed:', err);
    process.exit(1);
  });
}

module.exports = { runTests };
