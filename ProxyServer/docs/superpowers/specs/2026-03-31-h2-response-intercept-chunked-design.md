# Design: Certificates README + H2 Upstream + Response Interception + Chunked Awareness

**Date:** 2026-03-31

## Overview

Four deliverables for the ProxyServer project:

1. **Educational certificate README** — `docs/certificates.md` explaining TLS, MITM, cert generation with flow diagrams
2. **HTTP/2 upstream support** — ALPN negotiation with upstream servers; client still speaks HTTP/1.1 to proxy
3. **Response interception** — Wire existing `ResponseInterceptor` stub into both HTTP and HTTPS paths with UI support
4. **Chunked transfer-encoding awareness** — Proper chunked body parsing in TLS handler, plus keep-alive multi-request support

## 1. Certificate README

New file `docs/certificates.md` covering:
- Trust chain concept (Root CA → Leaf Cert → Browser validation)
- Normal TLS vs MITM TLS (two separate TLS sessions)
- ProxyServer's cert generation via node-forge (CA: RSA 2048, 10yr; Host: RSA 2048, 2yr, SAN)
- Trust installation per-platform with WHY explanations
- Security warnings about CA private key

## 2. HTTP/2 Upstream

In `tls-handler.js`, after decrypting client HTTP/1.1 from CONNECT tunnel:
- Try `http2.connect()` to upstream with ALPN
- If H2 negotiated, use H2 session stream for request/response
- If fallback, use existing raw TLS path
- Translate H2 response back to HTTP/1.1 for client
- Add `response.httpVersion` to TrafficEntry

## 3. Response Interception

- Buffer complete response before relaying to client
- Call `responseInterceptor.checkResponse()` after full response received
- If matched, hold via Promise (same pattern as request interception)
- Use distinct events: `forward-response` / `drop-response`
- New API routes: `POST /api/traffic/:id/forward-response`, `POST /api/traffic/:id/drop-response`
- Add `intercept.phase` to TrafficEntry (`'request'` | `'response'`)
- UI shows editable response fields when response is intercepted

## 4. Chunked Transfer-Encoding

- Parse `Transfer-Encoding: chunked` in TLS handler for both request and response bodies
- Read `<size>\r\n<data>\r\n` chunks until `0\r\n\r\n`
- After request/response cycle completes, reset parser state for keep-alive (multi-request per tunnel)
- Same decompression support as HTTP path (gzip/deflate/brotli)
