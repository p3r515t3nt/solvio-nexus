"""Background job subsystem (node execution plane,.

The node owns operational execution state ONLY (queue, running, retries, temporary
result + metadata). It is NEVER canonical memory, standing intent, user preference,
or authority. The Mac Core is the authoritative control plane.
"""
