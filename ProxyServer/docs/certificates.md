# How TLS Certificates Work: An Educational Guide

This document explains how TLS certificates work, how a MITM (Man-in-the-Middle) proxy uses them to inspect encrypted traffic, and how ProxyServer generates and manages certificates. It's written for reverse engineers and developers who want to understand the mechanics, not just follow a recipe.

## Table of Contents

1. [The Problem TLS Solves](#the-problem-tls-solves)
2. [How TLS Trust Works](#how-tls-trust-works)
3. [Certificate Anatomy](#certificate-anatomy)
4. [The Normal TLS Handshake](#the-normal-tls-handshake)
5. [How MITM Interception Works](#how-mitm-interception-works)
6. [How ProxyServer Generates Certificates](#how-proxyserver-generates-certificates)
7. [Trusting the CA Certificate](#trusting-the-ca-certificate)
8. [Security Implications](#security-implications)
9. [Troubleshooting](#troubleshooting)

---

## The Problem TLS Solves

Without TLS, HTTP traffic is plaintext. Anyone on the network path can read it:

```
Your Browser ──── plaintext ────► Router ──── plaintext ────► Server

                    Anyone here can read:
                    - URLs you visit
                    - Cookies / session tokens
                    - Form data / passwords
                    - API responses
```

TLS (Transport Layer Security, the protocol behind HTTPS) encrypts the connection so that only the two endpoints can read the data. But encryption alone isn't enough — you also need **authentication**. Without it, an attacker could pretend to be the server:

```
Your Browser ──── encrypted ────► Attacker ──── encrypted ────► Server
                                  (reads everything)

Without authentication, you can't tell if you're talking
to the real server or an impersonator.
```

TLS solves both problems: encryption (nobody can read the data in transit) and authentication (you know who you're talking to). Certificates are the mechanism for authentication.

---

## How TLS Trust Works

TLS authentication is built on a **chain of trust** rooted in Certificate Authorities (CAs).

### The Trust Chain

```
┌─────────────────────────────────────────────────────────────┐
│                    ROOT CERTIFICATE AUTHORITY                 │
│                                                              │
│  Your OS/browser ships with ~150 pre-trusted root CAs:      │
│  DigiCert, Let's Encrypt, Comodo, GlobalSign, etc.          │
│                                                              │
│  These are stored in your system's "trust store":            │
│    macOS:   Keychain Access → System Roots                   │
│    Linux:   /etc/ssl/certs/                                  │
│    Windows: certmgr.msc → Trusted Root CAs                  │
│    Firefox: Has its own built-in store (separate from OS)    │
│                                                              │
│  Root CA cert is self-signed: issuer == subject              │
└──────────────────────────┬──────────────────────────────────┘
                           │ signs
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                  INTERMEDIATE CERTIFICATE                     │
│                                                              │
│  Root CAs rarely sign leaf certs directly.                   │
│  Instead, they sign intermediate CAs, which sign leaves.     │
│  This limits damage if a key is compromised.                 │
│                                                              │
│  Issuer: Root CA                                             │
│  Subject: Intermediate CA                                    │
│  Basic Constraints: CA:TRUE                                  │
└──────────────────────────┬──────────────────────────────────┘
                           │ signs
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    LEAF CERTIFICATE                           │
│                                                              │
│  This is the certificate the server sends to your browser.   │
│                                                              │
│  Issuer: Intermediate CA                                     │
│  Subject: example.com                                        │
│  Subject Alternative Names (SAN): example.com, www.example…  │
│  Basic Constraints: CA:FALSE  (cannot sign other certs)      │
│  Key Usage: Digital Signature, Key Encipherment              │
│  Extended Key Usage: Server Authentication                   │
│                                                              │
│  The server also has the private key matching this cert.     │
│  The cert contains only the public key.                      │
└─────────────────────────────────────────────────────────────┘
```

### How Verification Works

When your browser connects to `https://example.com`, it:

```
Step 1: Server sends its leaf certificate (+ intermediates)

Step 2: Browser reads leaf cert
        ├── Subject Alternative Names include "example.com"?     ✓
        ├── Not expired?                                          ✓
        ├── Basic Constraints CA:FALSE?                           ✓
        └── Who signed it? → Intermediate CA

Step 3: Browser reads intermediate cert
        ├── Not expired?                                          ✓
        ├── Basic Constraints CA:TRUE?                            ✓
        └── Who signed it? → Root CA

Step 4: Browser checks trust store
        └── Root CA in trust store?                               ✓

Step 5: Browser verifies cryptographic signatures
        ├── Root CA's public key validates intermediate's sig?    ✓
        └── Intermediate's public key validates leaf's sig?       ✓

Result: TRUSTED — green padlock, connection proceeds
```

If ANY step fails, the browser shows a certificate error and (usually) refuses to connect.

---

## Certificate Anatomy

A TLS certificate is an X.509 data structure. Here are the fields that matter:

```
┌─────────────────────────────────────────────────────────┐
│  X.509 Certificate                                       │
│                                                          │
│  Version:             3 (v3 — modern, supports extensions)│
│  Serial Number:       unique identifier (hex)            │
│  Signature Algorithm: SHA-256 with RSA                   │
│                                                          │
│  ┌─ Issuer (who signed this cert) ──────────────────┐   │
│  │  CN = DigiCert SHA2 Secure Server CA              │   │
│  │  O  = DigiCert Inc                                │   │
│  └──────────────────────────────────────────────────-┘   │
│                                                          │
│  ┌─ Validity ────────────────────────────────────────┐   │
│  │  Not Before: 2025-01-01 00:00:00 UTC              │   │
│  │  Not After:  2026-01-01 23:59:59 UTC              │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Subject (who this cert identifies) ──────────────┐   │
│  │  CN = example.com                                  │   │
│  │  O  = Example Inc                                  │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Public Key ──────────────────────────────────────┐   │
│  │  Algorithm: RSA (2048 bit)                         │   │
│  │  Key: 30 82 01 0a 02 82 01 01 00 c4 ...           │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Extensions (v3) ────────────────────────────────-┐   │
│  │  Subject Alternative Name:                         │   │
│  │    DNS: example.com                                │   │
│  │    DNS: www.example.com                            │   │
│  │    DNS: *.example.com                              │   │
│  │  Basic Constraints: CA:FALSE                       │   │
│  │  Key Usage: Digital Signature, Key Encipherment    │   │
│  │  Extended Key Usage: TLS Web Server Authentication │   │
│  │  Authority Key Identifier: (links to issuer)       │   │
│  │  Subject Key Identifier: (hash of public key)      │   │
│  └───────────────────────────────────────────────────┘   │
│                                                          │
│  ┌─ Signature ──────────────────────────────────────-┐   │
│  │  The issuer's private key signed all the above.    │   │
│  │  Anyone with the issuer's public key can verify.   │   │
│  └───────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────┘
```

### Key Distinction: Public Key vs Private Key

```
┌──────────────────────┐     ┌───────────────────────────┐
│   PRIVATE KEY         │     │   PUBLIC KEY (in cert)     │
│                       │     │                            │
│ • NEVER leaves server │     │ • Freely shared            │
│ • Signs data          │     │ • Verifies signatures      │
│ • Decrypts data       │     │ • Encrypts data            │
│ • Proves identity     │     │ • Embedded in certificate  │
│                       │     │                            │
│ If compromised:       │     │ If leaked:                 │
│   Game over.          │     │   No problem — it's public │
│   Attacker can        │     │                            │
│   impersonate server  │     │                            │
└──────────────────────-┘     └───────────────────────────┘
```

---

## The Normal TLS Handshake

Here's what happens when your browser connects to `https://api.example.com`:

```
Browser                                              Server
   │                                                     │
   │──── ClientHello ──────────────────────────────────►│
   │     • TLS version: 1.3                              │
   │     • Supported cipher suites                       │
   │     • Random bytes                                  │
   │     • SNI: api.example.com                          │
   │                                                     │
   │◄──── ServerHello ─────────────────────────────────│
   │      • Selected cipher suite                        │
   │      • Server's random bytes                        │
   │                                                     │
   │◄──── Certificate ─────────────────────────────────│
   │      • Server's leaf cert (CN=api.example.com)      │
   │      • Intermediate cert chain                      │
   │                                                     │
   │  Browser validates:                                 │
   │  ✓ SAN matches api.example.com                      │
   │  ✓ Cert not expired                                 │
   │  ✓ Chain leads to trusted root CA                   │
   │  ✓ Signatures valid                                 │
   │                                                     │
   │──── Key Exchange ─────────────────────────────────►│
   │     (ECDHE: both sides derive shared secret)        │
   │                                                     │
   │◄═══ Encrypted Application Data ═══════════════════►│
   │     GET /api/users HTTP/1.1                         │
   │     Host: api.example.com                           │
   │     Authorization: Bearer eyJhbG...                 │
   │                                                     │
   │     ← 200 OK                                        │
   │     {"users": [...]}                                │
   │                                                     │

   An observer on the network sees:
   ✗ Cannot read the URL path (/api/users)
   ✗ Cannot read headers (Authorization token)
   ✗ Cannot read request or response bodies
   ✓ CAN see the SNI hostname (api.example.com)
   ✓ CAN see the IP address and port
   ✓ CAN see the amount of data transferred
```

---

## How MITM Interception Works

A MITM proxy creates **two separate TLS sessions** — one with the client and one with the upstream server — and relays plaintext between them.

### The CONNECT Tunnel

When a browser is configured to use an HTTP proxy and navigates to an HTTPS URL, it uses the HTTP `CONNECT` method to establish a tunnel:

```
Browser                         Proxy (:9080)                    Server
   │                                │                               │
   │── CONNECT api.example.com:443 ─►│                               │
   │   Host: api.example.com:443    │                               │
   │                                │                               │
   │◄── HTTP/1.1 200 Established ───│                               │
   │                                │                               │
   │   At this point the browser    │                               │
   │   thinks it has a raw TCP      │                               │
   │   tunnel to the server.        │                               │
   │   It starts TLS...             │                               │
```

### Without MITM (Blind Tunnel)

```
Browser                         Proxy                         Server
   │                               │                              │
   │══ TLS directly to server ═══════════════════════════════════►│
   │   (proxy just copies bytes)   │                              │
   │                               │                              │
   │   Proxy sees: encrypted blob  │                              │
   │   Proxy knows: hostname, port │                              │
   │   Proxy CANNOT see: anything  │                              │
```

### With MITM (ProxyServer's Approach)

```
Browser                         Proxy                            Server
   │                               │                                │
   │── CONNECT api.example.com:443►│                                │
   │◄── 200 Established ──────────│                                │
   │                               │                                │
   │   Browser starts TLS...       │                                │
   │                               │                                │
   │   ┌─────────────────────────────────────────────────────────┐  │
   │   │ Proxy generates a FAKE certificate:                      │  │
   │   │   Subject: api.example.com                               │  │
   │   │   SAN: api.example.com                                   │  │
   │   │   Issuer: ProxyServer CA  (our local CA)                 │  │
   │   │   Signed by: our CA's private key                        │  │
   │   └─────────────────────────────────────────────────────────┘  │
   │                               │                                │
   │                   TLS SESSION 1                                │
   │══ TLS handshake ════════════►│                                │
   │   Browser sees cert for       │                                │
   │   "api.example.com" signed    │                                │
   │   by "ProxyServer CA".        │                                │
   │                               │                                │
   │   Is ProxyServer CA trusted?  │                                │
   │   ├── YES (user added to      │                                │
   │   │   trust store) → proceed  │                                │
   │   └── NO → certificate error  │                                │
   │       (red warning page)      │                                │
   │                               │                                │
   │   Browser sends request:      │             TLS SESSION 2      │
   │                               │                                │
   │── GET /api/users ───────────►│══ TLS handshake ══════════════►│
   │   (encrypted with Session 1   │   (proxy connects as client    │
   │    keys — proxy decrypts)     │    to real server)             │
   │                               │                                │
   │          PROXY SEES           │── GET /api/users ─────────────►│
   │          PLAINTEXT            │   (encrypted with Session 2    │
   │          REQUEST              │    keys — server decrypts)     │
   │                               │                                │
   │                               │◄── 200 OK {"users": [...]} ───│
   │          PROXY SEES           │   (encrypted with Session 2    │
   │          PLAINTEXT            │    keys — proxy decrypts)      │
   │          RESPONSE             │                                │
   │                               │                                │
   │◄── 200 OK {"users": [...]} ──│                                │
   │   (encrypted with Session 1   │                                │
   │    keys — browser decrypts)   │                                │
   │                               │                                │

   The proxy has full visibility:
   ✓ Full URL, headers, cookies
   ✓ Request body (POST data, JSON, etc.)
   ✓ Response status, headers
   ✓ Response body (JSON, HTML, images, etc.)
   ✓ Can MODIFY any of the above before forwarding
```

### Why the Browser Accepts It

The fake certificate is trusted because:

1. It has `Subject Alternative Name: api.example.com` — matches the URL
2. It's signed by `ProxyServer CA` — which the user added to their trust store
3. The signature is cryptographically valid — our CA key signed it
4. All other checks pass (not expired, correct extensions)

The browser has no way to know it's not talking to the real server. From its perspective, the TLS handshake succeeded with a valid certificate for the correct hostname.

---

## How ProxyServer Generates Certificates

### Step 1: CA Certificate (First Run Only)

On first launch, ProxyServer creates a Certificate Authority:

```
┌─────────────────────────────────────────────────────┐
│  CA Certificate Generation (cert-manager.js)         │
│                                                      │
│  1. Generate RSA 2048-bit keypair                    │
│     └── Private key → certs/ca.key (PEM)            │
│     └── Public key  → embedded in cert              │
│                                                      │
│  2. Create X.509 certificate:                        │
│     ├── Serial: 01                                   │
│     ├── Validity: 10 years                           │
│     ├── Subject & Issuer (self-signed):              │
│     │   CN  = ProxyServer CA                         │
│     │   O   = ProxyServer                            │
│     │   C   = US                                     │
│     ├── Extensions:                                  │
│     │   basicConstraints: CA:TRUE  ← can sign certs │
│     │   keyUsage: keyCertSign, digitalSignature,     │
│     │            cRLSign                             │
│     │   subjectKeyIdentifier                         │
│     └── Sign with CA private key (SHA-256)           │
│                                                      │
│  3. Save → certs/ca.crt (PEM)                        │
│                                                      │
│  Subsequent runs: load existing CA from disk          │
└─────────────────────────────────────────────────────┘
```

### Step 2: Per-Host Leaf Certificate (On Demand)

When a CONNECT request arrives for a new hostname:

```
┌─────────────────────────────────────────────────────┐
│  Host Certificate Generation                         │
│                                                      │
│  Triggered by: CONNECT api.example.com:443           │
│                                                      │
│  1. Check memory cache (Map<hostname, {key, cert}>)  │
│     └── HIT → return cached cert, skip to TLS       │
│                                                      │
│  2. Check disk cache (certs/hosts/api.example.com.*) │
│     └── HIT → load from disk, add to memory cache   │
│                                                      │
│  3. MISS → Generate new certificate:                 │
│     ├── Generate RSA 2048-bit keypair                │
│     ├── Create X.509 cert:                           │
│     │   ├── Serial: random 16 bytes (hex)            │
│     │   ├── Validity: 2 years                        │
│     │   ├── Subject:                                 │
│     │   │   CN = api.example.com                     │
│     │   │   O  = ProxyServer                         │
│     │   ├── Issuer: (copied from CA cert subject)    │
│     │   │   CN = ProxyServer CA                      │
│     │   │   O  = ProxyServer                         │
│     │   │   C  = US                                  │
│     │   ├── Extensions:                              │
│     │   │   basicConstraints: CA:FALSE               │
│     │   │   keyUsage: digitalSignature,              │
│     │   │            keyEncipherment                 │
│     │   │   extKeyUsage: serverAuth                  │
│     │   │   subjectAltName:                          │
│     │   │     DNS: api.example.com  ← critical!     │
│     │   │   subjectKeyIdentifier                     │
│     │   │   authorityKeyIdentifier                   │
│     │   └── Sign with CA PRIVATE KEY (SHA-256)       │
│     │       (this is what makes browsers trust it)   │
│     │                                                │
│     ├── Save to certs/hosts/api.example.com.key      │
│     ├── Save to certs/hosts/api.example.com.crt      │
│     └── Add to memory cache                          │
│                                                      │
│  4. Return {key, cert} for TLS socket creation       │
└─────────────────────────────────────────────────────┘
```

### Why SAN Matters

Modern browsers (since ~2017) ignore the `CN` (Common Name) field and ONLY check `Subject Alternative Name` for hostname matching. If the SAN doesn't include the hostname, the browser rejects the cert even if CN matches:

```
✗ CN = api.example.com, no SAN     → Chrome/Firefox REJECT
✓ CN = api.example.com, SAN = api.example.com → ACCEPT
✓ CN = anything, SAN = api.example.com         → ACCEPT
```

ProxyServer always sets both CN and SAN to the hostname for maximum compatibility.

### Performance Note

`node-forge` generates RSA keys in pure JavaScript (no OpenSSL binding). A 2048-bit keypair takes ~200–500ms. That's why the proxy caches generated certs — the first request to a new host is slow, but all subsequent requests are instant.

```
First request to api.example.com:  ~300ms (keygen)
Second request to api.example.com: ~0ms   (memory cache)
After restart, first request:      ~5ms   (disk cache)
After deleting certs/hosts/:       ~300ms (regenerate)
```

---

## Trusting the CA Certificate

The generated CA certificate must be added to your system's trust store. Without this step, every HTTPS site will show a certificate error.

### macOS

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain certs/ca.crt
```

**What this does:**
- `-d` — add to the admin trust store (persistent across reboots)
- `-r trustRoot` — trust as a root certificate authority
- `-k /Library/Keychains/System.keychain` — system-wide (all users)

**To remove later:**
```bash
# Open Keychain Access → System → find "ProxyServer CA" → Delete
# Or:
sudo security remove-trusted-cert -d certs/ca.crt
```

### Firefox (All Platforms)

Firefox maintains its **own certificate store** separate from the OS. Even after trusting the CA in macOS Keychain, Firefox will still show errors.

```
Settings → Privacy & Security → Certificates → View Certificates
  → Authorities tab → Import → select certs/ca.crt
  → Check "Trust this CA to identify websites" → OK
```

**Why Firefox is different:** Mozilla decided that relying on the OS trust store is a security risk (malware can add CAs to the OS store). Firefox ships its own curated list and only trusts what you explicitly add.

### Chrome / Safari / curl (macOS)

These all use the macOS Keychain, so the `security add-trusted-cert` command above covers them.

### Linux

```bash
# Debian/Ubuntu
sudo cp certs/ca.crt /usr/local/share/ca-certificates/proxyserver-ca.crt
sudo update-ca-certificates

# RHEL/Fedora
sudo cp certs/ca.crt /etc/pki/ca-trust/source/anchors/proxyserver-ca.crt
sudo update-ca-trust
```

### Programmatic Clients

For `curl`, `wget`, Python `requests`, Node.js `https`, etc., you can either trust the CA system-wide (above) or specify it per-request:

```bash
# curl
curl --cacert certs/ca.crt https://api.example.com

# Or set environment variable
export SSL_CERT_FILE=certs/ca.crt
export NODE_EXTRA_CA_CERTS=certs/ca.crt
```

```python
# Python requests
import requests
requests.get("https://api.example.com", verify="certs/ca.crt")
```

```javascript
// Node.js
process.env.NODE_EXTRA_CA_CERTS = "certs/ca.crt";
// Or per-request:
https.get("https://api.example.com", { ca: fs.readFileSync("certs/ca.crt") });
```

---

## Security Implications

### The CA Private Key Is Extremely Sensitive

```
┌─────────────────────────────────────────────────────────┐
│  WARNING: certs/ca.key                                   │
│                                                          │
│  Anyone who has this file can:                           │
│  • Generate valid certificates for ANY hostname          │
│  • Intercept any HTTPS traffic on your machine           │
│  • Impersonate any website to your browser               │
│                                                          │
│  DO:                                                     │
│  ✓ Keep it local (it's in .gitignore)                    │
│  ✓ Delete it when you're done debugging                  │
│  ✓ Remove the CA from your trust store when done         │
│                                                          │
│  DO NOT:                                                 │
│  ✗ Commit it to version control                          │
│  ✗ Share it with anyone                                  │
│  ✗ Copy it to other machines                             │
│  ✗ Leave the CA trusted when not using the proxy         │
└─────────────────────────────────────────────────────────┘
```

### How to Clean Up

When you're done using the proxy:

```bash
# 1. Remove CA from macOS trust store
sudo security remove-trusted-cert -d certs/ca.crt

# 2. Remove from Firefox (manually via Settings)

# 3. Delete all generated certs
rm -rf certs/

# 4. Next time you run the proxy, a new CA is generated
#    (you'll need to trust it again)
```

### Certificate Pinning

Some applications implement **certificate pinning** — they only accept specific certificates or CAs, ignoring the system trust store. These apps will refuse to connect through a MITM proxy even after you trust the CA.

Common pinned applications:
- Most banking/financial apps
- Some messaging apps (Signal, WhatsApp)
- Apps using Android's Network Security Config with pin sets
- iOS apps using `NSAppTransportSecurity` with pinned keys

There is no workaround for certificate pinning without modifying the application binary. This is by design — pinning exists specifically to prevent MITM interception.

---

## Troubleshooting

### "Certificate is not trusted" / NET::ERR_CERT_AUTHORITY_INVALID

The CA certificate is not in your trust store. Follow the [trust instructions](#trusting-the-ca-certificate) for your platform.

### "Certificate is for a different domain"

The `Subject Alternative Name` in the generated cert doesn't match the hostname. This shouldn't happen with ProxyServer (it uses the exact CONNECT hostname), but if you see it, delete `certs/hosts/` and try again.

### Firefox shows errors but Chrome doesn't (or vice versa)

Firefox uses its own cert store. Trust the CA in both the OS keychain and Firefox's certificate manager.

### "Certificate has expired"

Host certificates have a 2-year validity. If you've been using the same proxy install for over 2 years, delete `certs/hosts/` to regenerate them. The CA certificate is valid for 10 years.

### First HTTPS request is slow (~300ms)

This is the RSA key generation time for the per-host certificate. Subsequent requests to the same host use the cached cert. If you're testing against a single API, only the very first request is slow.

### "SSL handshake failed" in the proxy logs

The client rejected the certificate. Common causes:
- CA not trusted (most common)
- App uses certificate pinning (see above)
- Client requires a specific TLS version not supported by the proxy
