/**
 * Stateless BYOK relay. Keys exist only in this invocation and the fixed
 * provider's Authorization header. Never add request logging, a shared-key
 * fallback, arbitrary upstream URLs, persistence, or automatic retries here.
 * Origin checks are browser isolation, not authentication or abuse prevention.
 */
export const LIMITS = Object.freeze({
  requestBytes: 1024 * 1024,
  chatResponseBytes: 2 * 1024 * 1024,
  imageResponseBytes: 12 * 1024 * 1024,
  requestTimeoutMs: 15_000,
  chatTimeoutMs: 120_000,
  imageTimeoutMs: 180_000,
  messages: 256,
  maxTokens: 65_536,
});

const UPSTREAMS = Object.freeze({
  deepseek: "https://api.deepseek.com/chat/completions",
  openai: "https://api.openai.com/v1/images/generations",
  seedream: "https://ark.cn-beijing.volces.com/api/v3/images/generations",
});
const ROLES = new Set(["system", "user", "assistant"]);
const EFFORTS = new Set(["none", "minimal", "low", "medium", "high", "xhigh", "max"]);
const QUALITY = new Set(["auto", "low", "medium", "high", "xhigh", "max", "standard", "hd"]);

class RelayError extends Error {
  constructor(status, code, message) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

function invalid(message) {
  return new RelayError(400, "invalid_request", message);
}

function object(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function apiHeaders(origin) {
  const headers = new Headers({
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store, private, max-age=0",
    "CDN-Cache-Control": "no-store",
    "Cloudflare-CDN-Cache-Control": "no-store",
    Pragma: "no-cache",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Resource-Policy": "same-origin",
    Vary: "Origin",
  });
  if (origin) headers.set("Access-Control-Allow-Origin", origin);
  return headers;
}

function errorResponse(error, origin) {
  // Never copy an upstream exception, URL, response body, or header into errors.
  const safe = error instanceof RelayError
    ? error
    : new RelayError(502, "relay_failed", "The request could not be completed. It was not retried.");
  return new Response(JSON.stringify({ error: { code: safe.code, message: safe.message } }), {
    status: safe.status,
    headers: apiHeaders(origin),
  });
}

function checkOrigin(request, url) {
  const origin = request.headers.get("Origin");
  const site = request.headers.get("Sec-Fetch-Site");
  if ((origin !== null && origin !== url.origin) || (site && !["same-origin", "none"].includes(site))) {
    throw new RelayError(403, "origin_not_allowed", "Use the API from this site's own page.");
  }
  // Non-browser clients can omit or spoof these headers; every request still
  // needs its own provider key. CORS does not stop consumption of Free quota.
  return origin;
}

function authorization(request) {
  const value = request.headers.get("Authorization") || "";
  if (!/^Bearer [A-Za-z0-9._~+\/=-]{1,4096}$/i.test(value)) {
    throw new RelayError(401, "key_required", "Supply your provider API key in the Authorization header.");
  }
  return value;
}

function modelName(value) {
  if (typeof value !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$/.test(value)) {
    throw invalid("A valid model identifier is required.");
  }
  return value;
}

function chatPayload(body) {
  if (body.provider !== undefined && body.provider !== "deepseek") {
    throw invalid("Chat supports the DeepSeek provider only.");
  }
  if (!Array.isArray(body.messages) || body.messages.length < 1 || body.messages.length > LIMITS.messages) {
    throw invalid("Supply between 1 and 256 text messages.");
  }
  const messages = body.messages.map((message) => {
    if (!object(message) || !ROLES.has(message.role) || typeof message.content !== "string") {
      throw invalid("Messages must have a system, user, or assistant role and text content.");
    }
    // No user-controlled tools, file IDs, image URLs, or nested metadata.
    return { role: message.role, content: message.content };
  });
  const payload = { model: modelName(body.model), messages, stream: false };
  const maxTokens = body.max_tokens === undefined ? 8192 : body.max_tokens;
  if (!Number.isInteger(maxTokens) || maxTokens < 1 || maxTokens > LIMITS.maxTokens) {
    throw invalid("max_tokens must be an integer between 1 and 65536.");
  }
  payload.max_tokens = maxTokens;
  if (body.response_format !== undefined) {
    if (!object(body.response_format) || !["text", "json_object"].includes(body.response_format.type)) {
      throw invalid("response_format.type must be text or json_object.");
    }
    payload.response_format = { type: body.response_format.type };
  }
  if (body.thinking !== undefined) {
    if (!object(body.thinking) || !["enabled", "disabled"].includes(body.thinking.type)) {
      throw invalid("thinking.type must be enabled or disabled.");
    }
    payload.thinking = { type: body.thinking.type };
  }
  if (body.reasoning_effort !== undefined) {
    if (!EFFORTS.has(body.reasoning_effort)) throw invalid("Unsupported reasoning_effort value.");
    payload.reasoning_effort = body.reasoning_effort;
  }
  if (payload.thinking && payload.reasoning_effort) {
    const enabled = payload.reasoning_effort !== "none";
    if (enabled !== (payload.thinking.type === "enabled")) {
      throw invalid("thinking and reasoning_effort must agree on whether thinking is enabled.");
    }
  }
  return payload;
}

function imagePayload(body) {
  if (!["openai", "seedream"].includes(body.provider)) {
    throw invalid("Image provider must be openai or seedream.");
  }
  const model = modelName(body.model);
  if (typeof body.prompt !== "string" || !body.prompt.trim() || body.prompt.length > 32_000) {
    throw invalid("Supply an image prompt between 1 and 32000 characters.");
  }
  const payload = { model, prompt: body.prompt };
  if (body.size !== undefined) {
    if (typeof body.size !== "string" || !/^(auto|[1-4]K|[1-9][0-9]{1,4}x[1-9][0-9]{1,4})$/.test(body.size)) {
      throw invalid("size must be auto, a supported K preset, or WIDTHxHEIGHT.");
    }
    payload.size = body.size;
  }
  if (body.quality !== undefined && !QUALITY.has(body.quality)) {
    throw invalid("Unsupported image quality value.");
  }
  if (body.provider === "openai") {
    // Request a single image. GPT image models return base64 automatically and
    // reject response_format. Only the legacy DALL-E API accepts that option.
    payload.n = 1;
    if (body.quality !== undefined) payload.quality = body.quality;
    if (["dall-e-2", "dall-e-3"].includes(model)) payload.response_format = "b64_json";
  } else {
    // Seedream documents b64_json. quality is not a Seedream generation field.
    payload.response_format = "b64_json";
    payload.stream = false;
    payload.sequential_image_generation = "disabled";
  }
  return { provider: body.provider, payload };
}

function createDeadline(milliseconds, parentSignal, timeoutError) {
  const controller = new AbortController();
  let rejectInterrupted;
  const interrupted = new Promise((_, reject) => { rejectInterrupted = reject; });
  // A streamed response can be waiting for its consumer rather than a read.
  // Still handle timeout rejection immediately, even without an active wait().
  void interrupted.catch(() => {});
  const stop = (error) => {
    if (controller.signal.aborted) return;
    controller.abort(error);
    rejectInterrupted(error);
  };
  const cancelled = () => stop(new RelayError(499, "request_cancelled", "The request was cancelled. It was not retried."));
  const timer = setTimeout(() => stop(timeoutError), milliseconds);
  parentSignal.addEventListener("abort", cancelled, { once: true });
  if (parentSignal.aborted) cancelled();
  return {
    signal: controller.signal,
    stop,
    wait: (pending) => Promise.race([interrupted, pending]),
    dispose() {
      clearTimeout(timer);
      parentSignal.removeEventListener("abort", cancelled);
    },
  };
}

async function withDeadline(milliseconds, parentSignal, timeoutError, operation) {
  const deadline = createDeadline(milliseconds, parentSignal, timeoutError);
  try {
    return await deadline.wait(operation(deadline.signal));
  } finally {
    deadline.dispose();
  }
}

function cancelBody(body) {
  if (body) void body.cancel().catch(() => {});
}

function checkDeclaredSize(message, limit, tooLarge) {
  const declaredLength = message.headers.get("Content-Length");
  if (declaredLength !== null && (!/^\d+$/.test(declaredLength) || Number(declaredLength) > limit)) {
    cancelBody(message.body);
    throw tooLarge;
  }
}

async function readBounded(message, limit, signal, tooLarge) {
  checkDeclaredSize(message, limit, tooLarge);
  signal.throwIfAborted();
  if (!message.body) return new Uint8Array();
  const reader = message.body.getReader();
  const cancel = () => { void reader.cancel().catch(() => {}); };
  signal.addEventListener("abort", cancel, { once: true });
  // Keep one growable byte buffer, not an array of chunks: a sender could
  // otherwise exhaust memory using millions of one-byte chunks within the cap.
  let bytes = new Uint8Array(Math.min(limit, 64 * 1024));
  let size = 0;
  try {
    while (true) {
      signal.throwIfAborted();
      const { done, value } = await reader.read();
      if (done) break;
      const nextSize = size + value.byteLength;
      if (nextSize > limit) {
        cancel();
        throw tooLarge;
      }
      if (nextSize > bytes.byteLength) {
        const expanded = new Uint8Array(Math.min(limit, Math.max(nextSize, bytes.byteLength * 2)));
        expanded.set(bytes.subarray(0, size));
        bytes = expanded;
      }
      bytes.set(value, size);
      size = nextSize;
    }
    signal.throwIfAborted();
    return bytes.subarray(0, size);
  } finally {
    signal.removeEventListener("abort", cancel);
    reader.releaseLock();
  }
}

function streamImages(upstream, deadline, tooLarge) {
  checkDeclaredSize(upstream, LIMITS.imageResponseBytes, tooLarge);
  deadline.signal.throwIfAborted();
  if (!upstream.body) {
    throw new RelayError(502, "invalid_provider_response", "The provider returned an empty response. The request was not retried.");
  }
  const reader = upstream.body.getReader();
  let output;
  let size = 0;
  let settled = false;
  const cleanup = () => {
    deadline.signal.removeEventListener("abort", aborted);
    deadline.dispose();
  };
  const cancelReader = () => {
    void reader.cancel().catch(() => {}).finally(() => reader.releaseLock());
  };
  const fail = (error) => {
    if (settled) return;
    settled = true;
    cleanup();
    output.error(error);
    deadline.stop(error);
    cancelReader();
  };
  const interrupted = () => new RelayError(502, "provider_stream_interrupted",
    "The image response was interrupted. It may already be charged; no retry was made.");
  const aborted = () => fail(interrupted());
  return new ReadableStream({
    start(controller) {
      output = controller;
      deadline.signal.addEventListener("abort", aborted, { once: true });
      if (deadline.signal.aborted) aborted();
    },
    async pull(controller) {
      try {
        // The abort listener cancels pending reads. Avoid adding a Promise.race
        // handler to one long-lived timeout promise for every streamed chunk.
        const { done, value } = await reader.read();
        if (settled) return;
        if (done) {
          settled = true;
          cleanup();
          reader.releaseLock();
          controller.close();
          return;
        }
        size += value.byteLength;
        if (size > LIMITS.imageResponseBytes) {
          fail(tooLarge);
          return;
        }
        // Pass bytes through with backpressure: no whole-image buffer, decode,
        // JSON.parse, or reserialization on the Free Worker. The browser must
        // validate the JSON and show fixed errors for malformed/error results.
        controller.enqueue(value);
      } catch {
        // Do not leak errors originating in the provider stream.
        fail(interrupted());
      }
    },
    cancel() {
      if (settled) return;
      settled = true;
      cleanup();
      deadline.stop(interrupted());
      cancelReader();
    },
  }, { highWaterMark: 0 });
}

function parseObject(bytes, error) {
  try {
    const value = JSON.parse(new TextDecoder("utf-8", { fatal: true }).decode(bytes));
    if (!object(value)) throw error;
    return value;
  } catch {
    throw error;
  }
}

function providerError(status) {
  if (status === 401 || status === 403) {
    return new RelayError(status, "provider_auth_failed", "The provider rejected the key or its permissions. Check your provider account.");
  }
  if (status === 402) return new RelayError(402, "provider_balance_required", "The provider requires available credit in your account.");
  if (status === 429) return new RelayError(429, "provider_rate_limited", "The provider's rate or quota limit was reached. The request was not retried.");
  if ([400, 404, 422].includes(status)) {
    return new RelayError(400, "provider_rejected_request", "The provider rejected the request. Check the model, options, and prompt against its documentation.");
  }
  return new RelayError(502, "provider_unavailable", "The provider could not complete the request. It was not retried.");
}

async function relay(request, origin, path) {
  const auth = authorization(request);
  if (!/^application\/json(?:\s*;|$)/i.test(request.headers.get("Content-Type") || "")) {
    throw new RelayError(415, "json_required", "Send an application/json request body.");
  }
  if (request.headers.has("Content-Encoding") && request.headers.get("Content-Encoding") !== "identity") {
    throw new RelayError(415, "encoding_not_supported", "Compressed request bodies are not supported.");
  }
  const inputBytes = await withDeadline(
    LIMITS.requestTimeoutMs,
    request.signal,
    new RelayError(408, "request_timeout", "The request body took too long to arrive."),
    (signal) => readBounded(request, LIMITS.requestBytes, signal, new RelayError(413, "request_too_large", "The request exceeds the 1 MiB limit.")),
  );
  const body = parseObject(inputBytes, invalid("Supply a valid JSON object."));
  const chat = path === "/api/chat";
  const { provider, payload } = chat ? { provider: "deepseek", payload: chatPayload(body) } : imagePayload(body);
  const timeout = chat ? LIMITS.chatTimeoutMs : LIMITS.imageTimeoutMs;
  const deadline = createDeadline(timeout, request.signal,
    new RelayError(504, "provider_timeout", "The provider request timed out. It may still be charged; check your provider before trying again."));
  let streaming = false;
  try {
    const signal = deadline.signal;
    signal.throwIfAborted();
    const upstream = await deadline.wait(fetch(UPSTREAMS[provider], {
      method: "POST",
      headers: { Authorization: auth, "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(payload),
      redirect: "manual",
      cache: "no-store",
      signal,
    }));
    if (upstream.status >= 300 && upstream.status < 400) {
      cancelBody(upstream.body);
      throw new RelayError(502, "provider_redirect_rejected", "The provider returned an unexpected redirect. It was not followed.");
    }
    if (!upstream.ok) {
      // Do not read untrusted error bodies: providers sometimes echo keys.
      cancelBody(upstream.body);
      throw providerError(upstream.status);
    }
    if (!/^application\/(?:[a-z0-9.+-]+\+)?json(?:\s*;|$)/i.test(upstream.headers.get("Content-Type") || "")) {
      cancelBody(upstream.body);
      throw new RelayError(502, "invalid_provider_response", "The provider did not return JSON. The request was not retried.");
    }
    const tooLarge = new RelayError(502, "provider_response_too_large", "The provider response exceeded the relay limit. It may already be charged; no retry was made.");
    if (!chat) {
      // Once streaming starts, a timeout, over-limit body, or upstream read
      // failure interrupts the body; the HTTP status can no longer change.
      const response = new Response(streamImages(upstream, deadline, tooLarge), {
        status: upstream.status, headers: apiHeaders(origin),
      });
      streaming = true;
      return response;
    }
    const bytes = await deadline.wait(readBounded(upstream, LIMITS.chatResponseBytes, signal, tooLarge));
    const result = parseObject(bytes, new RelayError(502, "invalid_provider_response", "The provider returned invalid JSON. The request was not retried."));
    if (result.error) throw providerError(502);
    return new Response(bytes, { status: upstream.status, headers: apiHeaders(origin) });
  } finally {
    // A streamed image owns its deadline until EOF, failure, or cancellation.
    if (!streaming) deadline.dispose();
  }
}

export default {
  async fetch(request, env = {}) {
    let origin = null;
    try {
      const url = new URL(request.url);
      if (!url.pathname.startsWith("/api/") && url.pathname !== "/api") {
        if (!["GET", "HEAD"].includes(request.method)) {
          return errorResponse(new RelayError(405, "method_not_allowed", "Static files support GET and HEAD only."), null);
        }
        return env.ASSETS ? await env.ASSETS.fetch(request) : errorResponse(new RelayError(404, "not_found", "The requested file was not found."), null);
      }
      origin = checkOrigin(request, url);
      if (!["/api/chat", "/api/images"].includes(url.pathname)) throw new RelayError(404, "not_found", "Unknown API route.");
      if (url.search) throw invalid("API query parameters are not supported. Send the key only in the Authorization header.");
      if (request.method === "OPTIONS") {
        const method = request.headers.get("Access-Control-Request-Method");
        const requestedHeaders = (request.headers.get("Access-Control-Request-Headers") || "")
          .split(",").map((header) => header.trim().toLowerCase()).filter(Boolean);
        if ((method && method !== "POST") || requestedHeaders.some((header) => !["authorization", "content-type"].includes(header))) {
          throw new RelayError(403, "preflight_not_allowed", "Only POST with Authorization and Content-Type is supported.");
        }
        const headers = apiHeaders(origin);
        headers.set("Access-Control-Allow-Methods", "POST, OPTIONS");
        headers.set("Access-Control-Allow-Headers", "Authorization, Content-Type");
        return new Response(null, { status: 204, headers });
      }
      if (request.method !== "POST") throw new RelayError(405, "method_not_allowed", "Use POST for this API route.");
      return await relay(request, origin, url.pathname);
    } catch (error) {
      return errorResponse(error, origin);
    }
  },
};
