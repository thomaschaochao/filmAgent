import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import worker, { LIMITS } from "../worker.mjs";

// All credentials here are intentionally fake fixtures. No tests use a real
// provider, read a local .env, or require deployment/account credentials.
const KEY = "test-only-never-a-real-provider-key";
const ORIGIN = "https://filmagent.example";
const CHAT = { model: "deepseek-flash", messages: [{ role: "user", content: "Write one shot." }], max_tokens: 128 };
const IMAGE = { provider: "openai", model: "gpt-image-1", prompt: "A quiet forest", size: "1024x1024", quality: "low" };
const completion = { id: "test-completion", choices: [{ message: { role: "assistant", content: "A wide shot." } }] };

function request(path = "/api/chat", body = CHAT, options = {}) {
  return new Request(ORIGIN + path, {
    method: "POST",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}`, Origin: ORIGIN, ...options.headers },
    body: JSON.stringify(body),
    ...Object.fromEntries(Object.entries(options).filter(([key]) => key !== "headers")),
  });
}

function jsonResponse(body = completion, options = {}) {
  return new Response(JSON.stringify(body), { status: 200, ...options, headers: { "Content-Type": "application/json", ...options.headers } });
}

function mockFetch(t, callback = () => jsonResponse()) {
  return t.mock.method(globalThis, "fetch", callback);
}

async function assertError(response, status, code) {
  assert.equal(response.status, status);
  const body = await response.json();
  assert.equal(body.error.code, code);
  assert.equal(typeof body.error.message, "string");
  assert.equal(JSON.stringify(body).includes(KEY), false);
  assert.equal(response.headers.get("Cache-Control"), "no-store, private, max-age=0");
  return body;
}

function streamingRequest(stream, headers = {}) {
  return new Request(ORIGIN + "/api/chat", {
    method: "POST", body: stream, duplex: "half",
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${KEY}`, Origin: ORIGIN, ...headers },
  });
}

test("chat uses the fixed DeepSeek endpoint and only the current request key", async (t) => {
  const fetch = mockFetch(t, (url, options) => {
    assert.equal(url, "https://api.deepseek.com/chat/completions");
    assert.deepEqual(options.headers, { Authorization: `Bearer ${KEY}`, "Content-Type": "application/json", Accept: "application/json" });
    assert.equal(options.method, "POST");
    assert.equal(options.redirect, "manual");
    assert.equal(options.cache, "no-store");
    assert.ok(options.signal instanceof AbortSignal);
    assert.deepEqual(JSON.parse(options.body), { ...CHAT, stream: false });
    assert.equal(options.body.includes(KEY), false);
    return jsonResponse();
  });
  const env = new Proxy({}, { get() { throw new Error("API calls must never read environment keys"); } });
  const response = await worker.fetch(request(), env);
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), completion);
  assert.equal(fetch.mock.callCount(), 1);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.equal(response.headers.get("Cloudflare-CDN-Cache-Control"), "no-store");
});

test("success forwards only JSON bytes and safe response headers", async (t) => {
  mockFetch(t, () => jsonResponse(completion, { headers: { "Set-Cookie": `key=${KEY}`, "X-Provider-Debug": KEY, "Cache-Control": "public" } }));
  const response = await worker.fetch(request());
  assert.equal(response.headers.get("Set-Cookie"), null);
  assert.equal(response.headers.get("X-Provider-Debug"), null);
  assert.equal(response.headers.get("Cache-Control"), "no-store, private, max-age=0");
  assert.equal(response.headers.get("X-Content-Type-Options"), "nosniff");
  assert.deepEqual(await response.json(), completion);
});

test("allowlist strips nested extras, credentials, user URLs, stream and tools", async (t) => {
  mockFetch(t, (_url, options) => {
    assert.deepEqual(JSON.parse(options.body), {
      ...CHAT, messages: [{ role: "user", content: "Write one shot." }], stream: false,
      response_format: { type: "json_object" }, thinking: { type: "enabled" }, reasoning_effort: "high",
    });
    return jsonResponse();
  });
  const response = await worker.fetch(request("/api/chat", {
    ...CHAT, provider: "deepseek", api_key: "ignored", base_url: "https://evil.example", stream: true,
    messages: [{ ...CHAT.messages[0], name: "ignored", image_url: "https://evil.example", tool_calls: [] }],
    response_format: { type: "json_object", schema: { unwanted: true } },
    thinking: { type: "enabled", url: "https://evil.example" }, reasoning_effort: "high", tools: [{ type: "web_search" }],
  }));
  assert.equal(response.status, 200);
});

test("missing max_tokens gets a bounded explicit default", async (t) => {
  mockFetch(t, (_url, options) => {
    assert.equal(JSON.parse(options.body).max_tokens, 8192);
    return jsonResponse();
  });
  assert.equal((await worker.fetch(request("/api/chat", { model: CHAT.model, messages: CHAT.messages }))).status, 200);
});

test("OpenAI GPT Image does not send unsupported response_format", async (t) => {
  mockFetch(t, (url, options) => {
    assert.equal(url, "https://api.openai.com/v1/images/generations");
    assert.deepEqual(JSON.parse(options.body), { model: IMAGE.model, prompt: IMAGE.prompt, size: IMAGE.size, quality: "low", n: 1 });
    return jsonResponse({ data: [{ b64_json: "dGVzdA==" }] });
  });
  const response = await worker.fetch(request("/api/images", { ...IMAGE, n: 10, response_format: "url", image: "https://evil.example", user: "ignored" }));
  assert.equal(response.status, 200);
  assert.deepEqual(await response.json(), { data: [{ b64_json: "dGVzdA==" }] });
});

test("legacy DALL-E response_format is explicitly base64", async (t) => {
  mockFetch(t, (_url, options) => {
    assert.equal(JSON.parse(options.body).response_format, "b64_json");
    return jsonResponse({ data: [{ b64_json: "dGVzdA==" }] });
  });
  const response = await worker.fetch(request("/api/images", { ...IMAGE, model: "dall-e-3", quality: "standard" }));
  assert.equal(response.status, 200);
  await response.body.cancel();
});

test("Seedream uses a fixed upstream, base64 and single-image settings", async (t) => {
  mockFetch(t, (url, options) => {
    assert.equal(url, "https://ark.cn-beijing.volces.com/api/v3/images/generations");
    assert.deepEqual(JSON.parse(options.body), {
      model: "doubao-seedream-4-0-250828", prompt: IMAGE.prompt, size: "2K",
      response_format: "b64_json", stream: false, sequential_image_generation: "disabled",
    });
    return jsonResponse({ data: [{ url: "https://provider.example/result.png" }] });
  });
  const response = await worker.fetch(request("/api/images", { ...IMAGE, provider: "seedream", model: "doubao-seedream-4-0-250828", size: "2K", sequential_image_generation: "auto" }));
  assert.equal(response.status, 200);
  assert.equal((await response.json()).data[0].url, "https://provider.example/result.png");
});

test("a request cannot reuse another invocation's provider key", async (t) => {
  const seen = [];
  mockFetch(t, (_url, options) => { seen.push(options.headers.Authorization); return jsonResponse(); });
  await worker.fetch(request());
  await worker.fetch(request("/api/chat", CHAT, { headers: { Authorization: "Bearer different-test-key" } }));
  const noKey = request();
  noKey.headers.delete("Authorization");
  await assertError(await worker.fetch(noKey, { DEEPSEEK_API_KEY: "must-not-read" }), 401, "key_required");
  assert.deepEqual(seen, [`Bearer ${KEY}`, "Bearer different-test-key"]);
});

for (const [label, body] of [
  ["missing model", { ...CHAT, model: undefined }],
  ["URL model", { ...CHAT, model: "https://evil.example" }],
  ["wrong provider", { ...CHAT, provider: "openai" }],
  ["no messages", { ...CHAT, messages: [] }],
  ["too many messages", { ...CHAT, messages: Array(257).fill(CHAT.messages[0]) }],
  ["tool role", { ...CHAT, messages: [{ role: "tool", content: "text" }] }],
  ["multimodal content", { ...CHAT, messages: [{ role: "user", content: [{ type: "image_url", image_url: "https://evil.example" }] }] }],
  ["too many tokens", { ...CHAT, max_tokens: LIMITS.maxTokens + 1 }],
  ["fractional tokens", { ...CHAT, max_tokens: 1.5 }],
  ["negative tokens", { ...CHAT, max_tokens: -1 }],
  ["wrong response format", { ...CHAT, response_format: { type: "json_schema" } }],
  ["wrong thinking", { ...CHAT, thinking: { type: "random" } }],
  ["wrong effort", { ...CHAT, reasoning_effort: "random" }],
  ["conflicting thinking and effort", { ...CHAT, thinking: { type: "disabled" }, reasoning_effort: "high" }],
  ["array root", [CHAT]],
  ["null root", null],
]) {
  test(`rejects invalid chat input: ${label}`, async (t) => {
    const fetch = mockFetch(t);
    await assertError(await worker.fetch(request("/api/chat", body)), 400, "invalid_request");
    assert.equal(fetch.mock.callCount(), 0);
  });
}

for (const [label, body] of [
  ["unknown provider", { ...IMAGE, provider: "https://evil.example" }],
  ["empty prompt", { ...IMAGE, prompt: "  " }],
  ["oversized prompt", { ...IMAGE, prompt: "a".repeat(32_001) }],
  ["non-text prompt", { ...IMAGE, prompt: {} }],
  ["bad size", { ...IMAGE, size: "https://evil.example" }],
  ["non-text size", { ...IMAGE, size: 1024 }],
  ["bad quality", { ...IMAGE, quality: "unlimited" }],
]) {
  test(`rejects invalid image input: ${label}`, async (t) => {
    const fetch = mockFetch(t);
    await assertError(await worker.fetch(request("/api/images", body)), 400, "invalid_request");
    assert.equal(fetch.mock.callCount(), 0);
  });
}

test("rejects malformed JSON without echoing raw text", async (t) => {
  const fetch = mockFetch(t);
  await assertError(await worker.fetch(request("/api/chat", CHAT, { body: `{${KEY}` })), 400, "invalid_request");
  assert.equal(fetch.mock.callCount(), 0);
});

test("rejects unsupported request content types and compression", async (t) => {
  const fetch = mockFetch(t);
  await assertError(await worker.fetch(request("/api/chat", CHAT, { headers: { "Content-Type": "text/plain" } })), 415, "json_required");
  await assertError(await worker.fetch(request("/api/chat", CHAT, { headers: { "Content-Encoding": "gzip" } })), 415, "encoding_not_supported");
  assert.equal(fetch.mock.callCount(), 0);
});

test("rejects missing or malformed keys rather than using body or environment keys", async (t) => {
  const fetch = mockFetch(t);
  for (const value of ["", "Basic abc", "Bearer", "Bearer one two", "Bearer " + "a".repeat(4097)]) {
    await assertError(await worker.fetch(request("/api/chat", { ...CHAT, api_key: KEY }, { headers: { Authorization: value } }), { DEEPSEEK_API_KEY: KEY }), 401, "key_required");
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("same-origin preflight allows only POST and the two required headers", async (t) => {
  const fetch = mockFetch(t);
  const response = await worker.fetch(new Request(ORIGIN + "/api/chat", { method: "OPTIONS", headers: {
    Origin: ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "authorization, Content-Type",
  } }));
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), ORIGIN);
  assert.equal(response.headers.get("Access-Control-Allow-Headers"), "Authorization, Content-Type");
  assert.equal(response.headers.get("Access-Control-Allow-Credentials"), null);
  assert.equal(fetch.mock.callCount(), 0);
});

test("cross-origin and same-site browser calls fail closed", async (t) => {
  const fetch = mockFetch(t);
  for (const headers of [
    { Origin: "https://evil.example" }, { Origin: "null" }, { Origin: "http://filmagent.example" },
    { Origin: ORIGIN + ".evil.example" }, { "Sec-Fetch-Site": "same-site" }, { "Sec-Fetch-Site": "cross-site" },
  ]) {
    const response = await worker.fetch(request("/api/chat", CHAT, { headers }));
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
    await assertError(response, 403, "origin_not_allowed");
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("no-Origin CLI/dev requests are accepted without wildcard CORS", async (t) => {
  mockFetch(t);
  const input = request();
  input.headers.delete("Origin");
  const response = await worker.fetch(input);
  assert.equal(response.status, 200);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), null);
});

test("preflight rejects unapproved headers, methods and foreign origins", async (t) => {
  const fetch = mockFetch(t);
  for (const headers of [
    { "Access-Control-Request-Headers": "X-Custom" },
    { "Access-Control-Request-Method": "PUT" },
    { Origin: "https://evil.example" },
  ]) {
    const response = await worker.fetch(new Request(ORIGIN + "/api/images", { method: "OPTIONS", headers: { Origin: ORIGIN, ...headers } }));
    assert.equal(response.status, 403);
  }
  assert.equal(fetch.mock.callCount(), 0);
});

test("API query strings, unknown paths, and unsupported methods never fetch", async (t) => {
  const fetch = mockFetch(t);
  await assertError(await worker.fetch(request("/api/chat?api_key=invalid-test-query")), 400, "invalid_request");
  await assertError(await worker.fetch(request("/api/download")), 404, "not_found");
  await assertError(await worker.fetch(new Request(ORIGIN + "/api/chat")), 405, "method_not_allowed");
  assert.equal(fetch.mock.callCount(), 0);
});

test("assets are served only through the ASSETS binding", async (t) => {
  const fetch = mockFetch(t);
  const input = new Request(ORIGIN + "/app.mjs");
  const seen = [];
  const env = { ASSETS: { async fetch(request) { seen.push(request); return new Response("export default 1"); } } };
  assert.equal(await (await worker.fetch(input, env)).text(), "export default 1");
  assert.deepEqual(seen, [input]);
  assert.equal(fetch.mock.callCount(), 0);
  await assertError(await worker.fetch(request("/app.mjs"), env), 405, "method_not_allowed");
  await assertError(await worker.fetch(new Request(ORIGIN + "/missing")), 404, "not_found");
});

test("advertised and actual request sizes are bounded before any provider call", async (t) => {
  const fetch = mockFetch(t);
  await assertError(await worker.fetch(request("/api/chat", CHAT, { headers: { "Content-Length": String(LIMITS.requestBytes + 1) } })), 413, "request_too_large");
  let cancelled = false;
  let count = 0;
  const stream = new ReadableStream({
    pull(controller) { count++; controller.enqueue(new Uint8Array(300_000)); },
    cancel() { cancelled = true; },
  });
  await assertError(await worker.fetch(streamingRequest(stream, { "Content-Length": "1" })), 413, "request_too_large");
  assert.equal(cancelled, true);
  assert.ok(count <= 6, "must stop reading as soon as actual bytes exceed the cap");
  assert.equal(fetch.mock.callCount(), 0);
});

test("request limit counts UTF-8 bytes rather than only characters", async (t) => {
  const fetch = mockFetch(t);
  await assertError(await worker.fetch(request("/api/chat", { ...CHAT, messages: [{ role: "user", content: "影".repeat(400_000) }] })), 413, "request_too_large");
  assert.equal(fetch.mock.callCount(), 0);
});

test("small stream chunks preserve UTF-8 and do not change the allowed payload", async (t) => {
  const input = { ...CHAT, messages: [{ role: "user", content: "电影 🎬" }] };
  const bytes = new TextEncoder().encode(JSON.stringify(input));
  let position = 0;
  mockFetch(t, (_url, options) => {
    assert.deepEqual(JSON.parse(options.body).messages, input.messages);
    return jsonResponse();
  });
  const stream = new ReadableStream({
    pull(controller) {
      if (position === bytes.length) controller.close();
      else controller.enqueue(bytes.subarray(position, ++position));
    },
  });
  assert.equal((await worker.fetch(streamingRequest(stream))).status, 200);
});

test("a pre-cancelled request never reaches the provider", async (t) => {
  const fetch = mockFetch(t);
  const controller = new AbortController();
  controller.abort();
  await assertError(await worker.fetch(request("/api/chat", CHAT, { signal: controller.signal })), 499, "request_cancelled");
  assert.equal(fetch.mock.callCount(), 0);
});

for (const status of [301, 302, 303, 307, 308]) {
  test(`provider redirect ${status} never follows or exposes credentials`, async (t) => {
    const fetch = mockFetch(t, () => new Response("redirect", { status, headers: { Location: `https://evil.example/${KEY}` } }));
    const response = await worker.fetch(request());
    await assertError(response, 502, "provider_redirect_rejected");
    assert.equal(response.headers.get("Location"), null);
    assert.equal(fetch.mock.callCount(), 1);
  });
}

for (const [providerStatus, relayStatus, code] of [
  [400, 400, "provider_rejected_request"], [401, 401, "provider_auth_failed"],
  [402, 402, "provider_balance_required"], [403, 403, "provider_auth_failed"],
  [429, 429, "provider_rate_limited"], [500, 502, "provider_unavailable"], [503, 502, "provider_unavailable"],
]) {
  test(`provider error ${providerStatus} is sanitized with no retry`, async (t) => {
    let cancelled = false;
    const fetch = mockFetch(t, () => new Response(new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode(JSON.stringify({ error: KEY }))); },
      cancel() { cancelled = true; },
    }), { status: providerStatus, headers: { "Content-Type": "application/json" } }));
    await assertError(await worker.fetch(request()), relayStatus, code);
    assert.equal(cancelled, true);
    assert.equal(fetch.mock.callCount(), 1);
  });
}

test("thrown provider failures cannot expose a key, URL, or stack", async (t) => {
  const fetch = mockFetch(t, () => { throw new Error(`secret ${KEY} at https://evil.example`); });
  const error = await assertError(await worker.fetch(request()), 502, "relay_failed");
  assert.equal(JSON.stringify(error).includes("evil.example"), false);
  assert.deepEqual(Object.keys(error.error).sort(), ["code", "message"]);
  assert.equal(fetch.mock.callCount(), 1);
});

test("invalid success content type and JSON are sanitized", async (t) => {
  for (const response of [
    new Response(KEY, { headers: { "Content-Type": "text/html" } }),
    new Response(KEY, { headers: { "Content-Type": "application/json" } }),
    jsonResponse([KEY]),
    new Response(new Uint8Array([255]), { headers: { "Content-Type": "application/json" } }),
  ]) {
    const fetch = mockFetch(t, () => response);
    await assertError(await worker.fetch(request()), 502, "invalid_provider_response");
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
});

test("a provider's error-shaped HTTP 200 is not passed through", async (t) => {
  mockFetch(t, () => jsonResponse({ error: { message: KEY } }));
  await assertError(await worker.fetch(request()), 502, "provider_unavailable");
});

test("image JSON is passed through for browser validation without Worker JSON.parse", async (t) => {
  const body = '{"data":[{"b64_json":"dGVzdA=="}]}';
  mockFetch(t, () => new Response(body, { headers: { "Content-Type": "application/json" } }));
  const originalParse = JSON.parse;
  const parse = t.mock.method(JSON, "parse", (text, ...rest) => {
    assert.ok(text.length < 1000, "only the small incoming request may be parsed");
    assert.equal(text.includes("b64_json"), false, "do not parse the image response");
    return originalParse(text, ...rest);
  });
  const response = await worker.fetch(request("/api/images", IMAGE));
  assert.equal(response.status, 200);
  assert.equal(await response.text(), body);
  assert.equal(parse.mock.callCount(), 1);
});

test("browser receives success image bytes unchanged and must validate malformed or error JSON", async (t) => {
  for (const body of ["malformed JSON", '{"data":[{"error":{"message":"test-error"}}]}']) {
    const fetch = mockFetch(t, () => new Response(body, { headers: { "Content-Type": "application/json" } }));
    const response = await worker.fetch(request("/api/images", IMAGE));
    assert.equal(response.status, 200);
    assert.equal(await response.text(), body);
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
});

test("a successful response exactly at the byte cap is accepted", async (t) => {
  const prefix = '{"text":"';
  const suffix = '"}';
  const bytes = prefix + "a".repeat(LIMITS.chatResponseBytes - prefix.length - suffix.length) + suffix;
  mockFetch(t, () => new Response(bytes, { headers: { "Content-Type": "application/json; charset=utf-8", "Content-Length": String(LIMITS.chatResponseBytes) } }));
  const response = await worker.fetch(request());
  assert.equal(response.status, 200);
  assert.equal((await response.arrayBuffer()).byteLength, LIMITS.chatResponseBytes);
});

test("advertised provider responses over each route limit are cancelled", async (t) => {
  for (const [path, input, limit] of [["/api/chat", CHAT, LIMITS.chatResponseBytes], ["/api/images", IMAGE, LIMITS.imageResponseBytes]]) {
    let cancelled = false;
    const fetch = mockFetch(t, () => new Response(new ReadableStream({ cancel() { cancelled = true; } }), {
      headers: { "Content-Type": "application/json", "Content-Length": String(limit + 1) },
    }));
    await assertError(await worker.fetch(request(path, input)), 502, "provider_response_too_large");
    assert.equal(cancelled, true);
    assert.equal(fetch.mock.callCount(), 1);
    fetch.mock.restore();
  }
});

test("chunked provider response limit is enforced even with a false content length", async (t) => {
  let cancelled = false;
  let chunks = 0;
  const fetch = mockFetch(t, () => new Response(new ReadableStream({
    pull(controller) { chunks++; controller.enqueue(new Uint8Array(500_000)); },
    cancel() { cancelled = true; },
  }), { headers: { "Content-Type": "application/json", "Content-Length": "1" } }));
  await assertError(await worker.fetch(request()), 502, "provider_response_too_large");
  assert.equal(cancelled, true);
  assert.ok(chunks <= 7);
  assert.equal(fetch.mock.callCount(), 1);
});

test("image streaming returns headers and the first chunk before upstream EOF without copying", async (t) => {
  let source;
  const first = new TextEncoder().encode('{"data":[{"b64_json":"');
  const last = new TextEncoder().encode('dGVzdA=="}]}');
  const fetch = mockFetch(t, () => new Response(new ReadableStream({ start(controller) { source = controller; } }), {
    headers: { "Content-Type": "application/json" },
  }));
  const response = await worker.fetch(request("/api/images", IMAGE));
  assert.equal(response.status, 200, "headers must arrive without waiting for a full image");
  source.enqueue(first);
  const reader = response.body.getReader();
  const part = await reader.read();
  assert.equal(part.value, first, "forward the original chunk without copying it");
  source.enqueue(last);
  source.close();
  assert.equal((await reader.read()).value, last);
  assert.equal((await reader.read()).done, true);
  assert.equal(fetch.mock.callCount(), 1);
});

test("image actual byte cap interrupts an already-started response and aborts upstream", async (t) => {
  let cancelled = false;
  let signal;
  const chunk = new Uint8Array(1024 * 1024);
  const fetch = mockFetch(t, (_url, options) => {
    signal = options.signal;
    return new Response(new ReadableStream({
      pull(controller) { controller.enqueue(chunk); }, cancel() { cancelled = true; },
    }), { headers: { "Content-Type": "application/json", "Content-Length": "1" } });
  });
  const response = await worker.fetch(request("/api/images", IMAGE));
  assert.equal(response.status, 200);
  let received = 0;
  const reader = response.body.getReader();
  await assert.rejects(async () => {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      received += value.byteLength;
    }
  }, (error) => error.code === "provider_response_too_large" && !error.message.includes(KEY));
  assert.equal(received, LIMITS.imageResponseBytes);
  assert.equal(cancelled, true);
  assert.equal(signal.aborted, true);
  assert.equal(fetch.mock.callCount(), 1);
});

test("image upstream read failures produce only fixed stream errors", async (t) => {
  let source;
  let signal;
  const fetch = mockFetch(t, (_url, options) => {
    signal = options.signal;
    return new Response(new ReadableStream({ start(controller) { source = controller; } }), { headers: { "Content-Type": "application/json" } });
  });
  const response = await worker.fetch(request("/api/images", IMAGE));
  source.error(new Error(`provider echoed ${KEY}`));
  await assert.rejects(response.text(), (error) => error.code === "provider_stream_interrupted" && !error.message.includes(KEY));
  assert.equal(signal.aborted, true);
  assert.equal(fetch.mock.callCount(), 1);
});

test("image downstream cancellation aborts the upstream and releases its reader", async (t) => {
  let cancelled = false;
  let signal;
  const upstream = new ReadableStream({ cancel() { cancelled = true; } });
  const fetch = mockFetch(t, (_url, options) => {
    signal = options.signal;
    return new Response(upstream, { headers: { "Content-Type": "application/json" } });
  });
  const response = await worker.fetch(request("/api/images", IMAGE));
  await response.body.cancel(`untrusted cancellation reason ${KEY}`);
  // Reader cancellation is best-effort and must not make downstream hang.
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(cancelled, true);
  assert.equal(upstream.locked, false);
  assert.equal(signal.aborted, true);
  assert.equal(signal.reason.message.includes(KEY), false);
  assert.equal(fetch.mock.callCount(), 1);
});

test("image request cancellation after headers interrupts the stream", async (t) => {
  const controller = new AbortController();
  let cancelled = false;
  mockFetch(t, () => new Response(new ReadableStream({ cancel() { cancelled = true; } }), { headers: { "Content-Type": "application/json" } }));
  const response = await worker.fetch(request("/api/images", IMAGE, { signal: controller.signal }));
  const rejected = assert.rejects(response.text(), (error) => error.code === "provider_stream_interrupted");
  controller.abort();
  await rejected;
  assert.equal(cancelled, true);
});

for (const consume of [true, false]) {
  test(`image stream timeout survives returned headers (consumer active: ${consume})`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    let cancelled = false;
    let signal;
    const fetch = mockFetch(t, (_url, options) => {
      signal = options.signal;
      return new Response(new ReadableStream({ cancel() { cancelled = true; } }), { headers: { "Content-Type": "application/json" } });
    });
    const response = await worker.fetch(request("/api/images", IMAGE));
    assert.equal(response.status, 200);
    const body = consume ? response.text() : null;
    t.mock.timers.tick(LIMITS.imageTimeoutMs);
    await assert.rejects(body || response.text(), (error) => error.code === "provider_stream_interrupted");
    assert.equal(signal.aborted, true);
    assert.equal(cancelled, true);
    assert.equal(fetch.mock.callCount(), 1);
  });
}

test("normal image EOF clears its timer and client abort listener", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const controller = new AbortController();
  let signal;
  mockFetch(t, (_url, options) => {
    signal = options.signal;
    return jsonResponse({ data: [{ b64_json: "dGVzdA==" }] });
  });
  const response = await worker.fetch(request("/api/images", IMAGE, { signal: controller.signal }));
  await response.text();
  t.mock.timers.tick(LIMITS.imageTimeoutMs);
  controller.abort();
  assert.equal(signal.aborted, false);
});

test("a stalled request upload has a finite deadline", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const fetch = mockFetch(t);
  let cancelled = false;
  const pending = worker.fetch(streamingRequest(new ReadableStream({ cancel() { cancelled = true; } })));
  t.mock.timers.tick(LIMITS.requestTimeoutMs);
  await assertError(await pending, 408, "request_timeout");
  assert.equal(cancelled, true);
  assert.equal(fetch.mock.callCount(), 0);
});

for (const [path, body, timeout] of [["/api/chat", CHAT, LIMITS.chatTimeoutMs], ["/api/images", IMAGE, LIMITS.imageTimeoutMs]]) {
  test(`${path} provider timeout aborts exactly one attempt`, async (t) => {
    t.mock.timers.enable({ apis: ["setTimeout"] });
    let started;
    const ready = new Promise((resolve) => { started = resolve; });
    let signal;
    const fetch = mockFetch(t, (_url, options) => { signal = options.signal; started(); return new Promise(() => {}); });
    const pending = worker.fetch(request(path, body));
    await ready;
    t.mock.timers.tick(timeout);
    await assertError(await pending, 504, "provider_timeout");
    assert.equal(signal.aborted, true);
    assert.equal(fetch.mock.callCount(), 1);
  });
}

test("timeout also applies while receiving the provider body", async (t) => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let reading;
  const ready = new Promise((resolve) => { reading = resolve; });
  let cancelled = false;
  mockFetch(t, () => new Response(new ReadableStream({
    pull() { reading(); }, cancel() { cancelled = true; },
  }, { highWaterMark: 0 }), { headers: { "Content-Type": "application/json" } }));
  const pending = worker.fetch(request());
  await ready;
  t.mock.timers.tick(LIMITS.chatTimeoutMs);
  await assertError(await pending, 504, "provider_timeout");
  assert.equal(cancelled, true);
});

test("client cancellation aborts the in-flight request without retry", async (t) => {
  const controller = new AbortController();
  let started;
  const ready = new Promise((resolve) => { started = resolve; });
  let signal;
  const fetch = mockFetch(t, (_url, options) => { signal = options.signal; started(); return new Promise(() => {}); });
  const pending = worker.fetch(request("/api/chat", CHAT, { signal: controller.signal }));
  await ready;
  controller.abort();
  await assertError(await pending, 499, "request_cancelled");
  assert.equal(signal.aborted, true);
  assert.equal(fetch.mock.callCount(), 1);
});

test("deployment is static assets plus a Worker with observability disabled", async () => {
  const config = JSON.parse(await readFile(new URL("../wrangler.jsonc", import.meta.url), "utf8"));
  assert.equal(config.main, "worker.mjs");
  assert.equal(config.assets.directory, "./public");
  assert.deepEqual(config.assets.run_worker_first, ["/api", "/api/*"]);
  assert.deepEqual(config.observability, { enabled: false, logs: { enabled: false, invocation_logs: false }, traces: { enabled: false } });
  for (const binding of ["vars", "kv_namespaces", "d1_databases", "r2_buckets", "queues", "durable_objects", "workflows", "vectorize", "ai", "unsafe", "limits"]) {
    assert.equal(config[binding], undefined, `no ${binding} binding/configuration in the Free baseline`);
  }
  const source = await readFile(new URL("../worker.mjs", import.meta.url), "utf8");
  assert.equal(/console\.(log|error|warn|info|debug)\s*\(/.test(source), false);
  const headers = await readFile(new URL("../public/_headers", import.meta.url), "utf8");
  assert.match(headers, /connect-src 'self'/);
  assert.match(headers, /script-src 'self'/);
  assert.doesNotMatch(headers, /unsafe-inline|unsafe-eval/);
});
