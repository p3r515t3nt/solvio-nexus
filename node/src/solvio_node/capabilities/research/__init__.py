"""Research capabilities.

Currently: research.fetch_public_url — a read-only, SSRF-guarded fetch of a single
public http(s) URL. Results are DATA only; the Mac Core classifies them as
UNTRUSTED_WEB. No search engine, browser, JavaScript, or crawling.
"""
