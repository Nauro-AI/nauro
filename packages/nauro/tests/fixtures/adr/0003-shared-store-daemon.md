# ADR 0003: Use a store-owned daemon for shared store access

## Status

Accepted

## Context

Several agents write to one embedded key-value store. With more than one process
opening a handle to the same embedded data directory, the service has multiple
OS processes competing to own the same local store, and no clear multi-process
coordination boundary.

The service also owns concerns above raw storage:

- schema validation and domain namespaces
- redaction before persistence
- idempotent agent writes
- request admission, backpressure, and operational health

A generic database pool in front of embedded handles would not solve the real
problem. It would multiply store owners instead of defining one owner.

## Decision

Introduce a single store-owned local daemon as the primary shared writer and
query service for multi-agent workflows.

The daemon opens exactly one embedded store handle for a configured data
directory and acquires an OS-level exclusive lease for that directory before
serving requests. The CLI and other clients send requests to the daemon instead
of independently opening the same store.

Do not build a pool of embedded handles against the same data directory.

## Alternatives Considered

### Keep CLI-Only Embedded Access

This is the simplest short-term shape, but it is unsafe as the default
multi-agent architecture. It relies on every caller behaving well and gives no
central place for request ordering, idempotency, or admission control.

### Add a Local File Lock Around CLI Writes

This is a useful emergency guard and should still be added for the embedded
fallback. It prevents the worst concurrent writers, but it does not create a
good shared read/write service. Agents would still repeatedly start processes,
rebuild local state, and miss daemon-level observability.

### Use the Embedded Engine's Daemon Directly

This may become a valid storage transport if the embedded engine's own daemon
exposes the needed primitives. The service still needs its own workflow layer
for schemas, redaction, provenance, and session semantics, so direct access
should be an adapter target, not the default surface.

## Consequences

Positive:

- Multi-agent writes have one serialization and recovery boundary.
- The CLI stays ergonomic while becoming a thin client for shared use.

Negative:

- The service now has a local daemon lifecycle to manage.
- The daemon protocol becomes a compatibility surface and needs tests.
