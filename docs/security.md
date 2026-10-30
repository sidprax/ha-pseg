# Security Policy & Hardening Guide

This document outlines the security controls and defensive practices for the Home Assistant PSE&G integration (`ha-pseg`).

## Threat Model & Defenses

1. **Companion Auth Server URL Validation**:
   - `auth_server_url` is strictly validated to prevent plaintext credential exposure and Server-Side Request Forgery (SSRF).
   - Only `http` and `https` schemes are permitted.
   - Unencrypted `http` is restricted to local loopback (`127.0.0.1`, `localhost`), private RFC1918 networks, and local Home Assistant container hostnames.
   - Any remote or public host must use encrypted `https`.

2. **Sensitive Information Redaction in Error Logs**:
   - Raw dictionary dumps from authentication services are sanitized before logging.
   - User credentials, session cookies, and request verification tokens are excluded from logs.

3. **Service Input Validation Gate**:
   - The `pseg.backfill_history` service strictly verifies date parameters (`YYYY-MM-DD`).
   - Invalid dates or malformed payloads are rejected immediately with clean error messages rather than causing unhandled runtime exceptions.

4. **Account Lockout & Rapid-Retry Safeguards**:
   - Silent automated re-authentication enforces cooldown timers and backoff limits.
   - Repeated authentication failures halt automated headless login attempts until the user reconfigures, preventing utility account lockout.

## Sentinel Security Learnings

### 2026-10-30 - Auth Endpoint SSRF and Credential Exposure
- **Vulnerability:** Unrestricted `auth_server_url` configurations could cause plaintext credentials to be transmitted over unencrypted HTTP to external endpoints or probe internal networks.
- **Learning:** Configuration options specifying companion service endpoints must enforce encryption boundaries and scheme validation.
- **Prevention:** Enforce strict URL parsing: only permit loopback/LAN on `http` and require `https` for all remote endpoints.
