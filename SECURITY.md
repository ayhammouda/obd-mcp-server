# Security policy

## Supported versions

The project is pre-1.0. Only the latest release and current `main` receive
security fixes.

## Reporting

The intended channel is GitHub's private **Report a vulnerability** flow for
`ayhammouda/obd-mcp-server` after the public repository exists and the owner
has enabled and verified private vulnerability reporting.

At this pre-publication stage, no private security email or verified repository
reporting channel is published. Configuring and testing that channel is a
first-release blocker. Until it exists, do not put exploit details, vehicle
identifiers, captures, credentials, or unsafe commands in a public issue.
After the repository exists, a reporter may open a minimal public issue asking
the maintainer to establish private contact, without including sensitive
details.

Include:

- affected version/commit and platform;
- driver, adapter, and transport;
- expected versus observed safety boundary;
- whether a real vehicle was involved;
- minimal sanitized reproduction;
- potential impact.

Do not test on a moving vehicle, bypass a gateway, clear faults, trigger
actuators, or publish commands that change ECU state.

## Response targets

- acknowledgement: 3 business days;
- initial assessment: 7 business days;
- remediation timeline: based on severity and vehicle exposure.

These are best-effort targets for a community project, not a service-level
agreement.

## Scope

High-priority reports include:

- any path to a mutable vehicle command;
- bypass of service/profile allowlists;
- non-loopback exposure or DNS-rebinding bypass;
- VIN, credential, private-profile, or capture disclosure;
- unsafe concurrency or response-matching behavior;
- malicious package/profile execution;
- release artifact or CI supply-chain compromise.

Incorrect diagnostic interpretation without a code or data-integrity defect is
usually a quality issue, but safety-critical mislabeling should still be
reported privately first.
