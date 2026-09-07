# Installed-client decision reference integration

This local candidate adds an explicit binding for the existing propose_decision tool. Normal stdio startup, the generated propose-decision CLI command, production tool schemas, local writes and sync behavior are unchanged. There is no environment activation flag or new public tool suite. Browser approval and other write families are outside this change.

## Binding and protocol

An operator constructs DecisionReferenceTransport with the reviewed HTTPS MCP endpoint, synthetic project, expected actor, HTTP client and credential reader. bind_decision_reference replaces only the existing propose_decision registration on the supplied MCP server instance. It refuses a missing original registration. Neither module is imported by normal startup. The isolated driver calls this binding explicitly.

Before a tool call, the transport initializes MCP and requires the isolated registry to advertise the exact prepared-reference schema. A legacy single-call propose_decision schema is refused before tools/call. Credentials must belong to the configured actor; the server still verifies signature, issuer, audience, scope and owner admission. Constructor configuration is operator-owned, not a tool argument or a capability claim.

The bound tool uses the server's existing names and modes: prepare, submit, discover, recover and explicit retry. It refuses content, base or capability fields on reference-bearing calls, including explicit null replacement fields. No caller-generated identity is accepted by preparation. Unknown fields do not fall back to local writes or raw judgment submission.

Responses are bounded and checked for duplicate JSON keys, exact canonical request bytes/digest, project/actor/reference binding, effective draft, admission state and the original 24-hour execution deadline. The installed judgment receipt verifier checks execution evidence. Stale and unresolved are retained as distinct outcomes. Discovery returns a page of exact requests without selecting or executing one. There is no local identity cache to lose; a new client can discover the server record after either response is lost.

The operator must present the complete effective draft and base, end the turn and obtain explicit approval in the owner's next reply. Only then submit its reference and digest. Stale requires a new prepared request and fresh approval. The software does not independently prove that a human approved. An explicit retry asks the existing server to perform its original-identity lookup and deadline checks. The adapter never retries automatically.

## Local proof

Install this candidate's nauro and nauro-core packages and the paired server with test dependencies in a dedicated environment. No normal installation needs replacement. Run the cross-repository test explicitly, with the paired server checkout on PYTHONPATH:

```sh
PYTHONPATH="$SERVER_CHECKOUT" "$TEST_PYTHON" -m pytest integration/test_decision_reference_server.py -q
```

The integration suite requires the actual client package and server fixtures. Missing dependencies fail collection; the check does not silently skip. It exercises the registered client tool against the existing local isolated application, signed test JWTs and synthetic mocked AWS storage. Tests cover lost preparation/commit responses, new-client discovery, stale reapproval, revoked owner, duplicate inert drafts, replacement fields and corrupt evidence. A separate client resumes the retry at the reservation race while the delayed original dispatch returns pending; one exact receipt remains recoverable. This is local evidence, not AWS contention or Claude.ai proof.

Normal startup and command regression tests verify that the dormant binding does not change existing behavior. The existing controlled-caller tests remain useful supporting evidence but are not a second product or deployment.

## Controlled hosted driver

scripts/controlled_decision_reference.py performs at most one selected tool call per run, after initialization and tool discovery. It uses the installed client registration above. It defaults to validation only, with no HTTP client or credential access. It is an operator test script, not a new Nauro command family.

Inputs:

- A reviewed manifest with endpoint, project_id, actor_id and isolated=true. The script accepts only the existing decision-probe.nauro.ai endpoint.
- A JSON file containing one exact tool request, including project_id. Prepare uses normal content fields. Submit/recover/retry use only request_mode, operation_id and payload_digest. Discover uses request_mode and optionally after.
- An unused evidence path. Execution creates it exclusively, records the request and flushes it before network access. A rerun cannot overwrite prior evidence.
- For execution only, a dedicated owner-only credential file with user_id and access_token. Obtain a valid token from the configured authorization server. Do not change normal client credentials or use test-signed tokens against the deployment.

Dry validation:

```sh
"$TEST_PYTHON" scripts/controlled_decision_reference.py \
  --manifest "$PROBE_MANIFEST" --request "$REQUEST_FILE" --evidence "$NEW_EVIDENCE_FILE"
```

After separate hosted-write authorization and the normal exact-draft approval, add --execute and --credentials "$PROBE_CREDENTIAL_FILE". Each run records the exact verified response, initialization and tool duration, HTTP status and available AWS request IDs. It does not log bearer tokens. HTTP errors leave an unresolved execution record and send no retry. No hosted run is authorized by this document.

For the controlled lost-response phase, --discard-response is permitted only with submit. It stores the verified result in private operator evidence but withholds it from caller output and exits 3. Start a fresh process with discover, then recover the explicitly selected request. This simulates caller response loss after receipt; it does not simulate a dropped network packet or prove connector retry behavior. The deployed server loss setting remains unchanged.

Run two separately approved same-base submissions in separate processes to test contention. Do not claim overlap inside Lambda merely because both processes start together. Inspect authoritative allocation, claim, saga and generation evidence. A reserved counter is not a publication. Retain pending outcomes; do not erase, automatically retry or roll back them.

## Limits and next gate

Auth0 may still block hosted credentials. No bypass, shared-audience change, deployment, seed change or hosted invocation is included. This preparation proves installed candidate code in a local application, not a released client or supported production rollout. Claude.ai new-chat recovery remains a separate real-host milestone. Small hosted samples are timing observations, not production p99 proof. Keep the decision-only boundary and production single-writer interim.
