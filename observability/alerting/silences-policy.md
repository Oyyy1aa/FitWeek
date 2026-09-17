# Silence policy

Silences are internal-only, have timezone-aware start/end times, and expire within
24 hours. Matchers use only the controlled alert/severity/component/environment/
outcome labels. User and resource identifiers are prohibited. Security-critical
alerts ignore ordinary silences. Phase 8B2 exposes no public Silence write API.
