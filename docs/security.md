# Security Policy & Hardening Guide

This document outlines the security controls and defensive practices for the Home Assistant PSE&G integration (`ha-pseg`).

## Threat Model & Defenses

1. **Companion Auth Server URL Validation**:
   - `auth_server_url` validates scheme, hostname, and transport encryption boundaries.
   - Only `http` and `https` schemes are permitted; unsupported schemes (`file://`, `gopher://`, etc.) are rejected.
   - Plaintext `http` is restricted to local loopback (`127.0.0.1`, `localhost`), RFC1918 private LAN ranges, Tailscale CGNAT addresses (`100.64.0.0/10`), single-label local hostnames (e.g. `nas`, `pseg_auth`), and local domain suffixes (`.local`, `.lan`, `.home.arpa`, `.internal`).
   - Remote/public hosts must use encrypted `https`.
   - Credential POST requests disable automatic redirects (`allow_redirects=False`) to prevent credential leakage via HTTP 307/308 redirects.
   - Embedded basic-auth credentials in URLs are stripped before logging to prevent accidental credential leakage in logs.
   - *Scope Note*: Because Home Assistant administrators configure these endpoints, this validation safeguards transport encryption and prevents unintended cleartext exposure rather than serving as full network-isolated SSRF sandboxing.

2. **Sensitive Information Redaction in Error Logs**:
   - Raw dictionary dumps from authentication services are sanitized before logging.
   - User credentials, session cookies, and request verification tokens are excluded from logs.
   - Any URLs logged in warning or diagnostic messages are stripped of embedded username/password credentials via `sanitize_url_for_logging`.

3. **Service Input Validation Gate**:
   - The `pseg.backfill_history` service strictly verifies date parameters (`YYYY-MM-DD`) via calendar date parsing (`datetime.strptime`).
   - Invalid dates (e.g., `2026-13-45`) or malformed payloads are rejected immediately with clean error messages rather than causing unhandled runtime exceptions.

4. **Account Lockout & Rapid-Retry Safeguards**:
   - Silent automated re-authentication enforces cooldown timers (60s minimum interval) and backoff limits.
   - Lockout guards use an asynchronous lock (`asyncio.Lock`) so concurrent coordinator updates await an in-flight re-authentication instead of prematurely failing.
   - Repeated authentication failures halt automated headless login attempts until the user reconfigures (maximum 3 consecutive failures), preventing utility account lockout.
   - The failure counter only increments when the companion service responds with a genuine portal authentication failure (network disconnects, companion downtime, and MFA prompts do not burn the failure budget).

## Sentinel Security Learnings

### 2026-10-30 - Auth Endpoint Validation and Credential Exposure
- **Vulnerability:** Unrestricted `auth_server_url` configurations could cause plaintext credentials to be transmitted over unencrypted HTTP to external endpoints or follow redirects to untrusted destinations.
- **Learning:** Configuration options specifying companion service endpoints must enforce encryption boundaries, scheme validation, redirect containment, and URL log sanitization.
- **Prevention:** Enforce strict URL parsing (only permit loopback/LAN on `http` and require `https` for remote endpoints), disable automatic redirects on credential POSTs, and strip basic auth credentials before logging.
