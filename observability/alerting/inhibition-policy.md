# Inhibition policy

- A firing `FitWeekCalendarWriteCircuitOpen` inhibits
  `FitWeekCalendarWriteFailureHigh` for the same low-cardinality component.
- `FitWeekApiUnavailable` may inhibit a lower-level `FitWeekHttpEndpointFailure`.
- Observability degradation never inhibits a security alert.
- Inhibition changes notification delivery only; alert state and business state are
  unchanged.
