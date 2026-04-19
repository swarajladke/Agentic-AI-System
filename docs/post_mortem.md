# Post-Mortem: Agentic AI Multi-Step System

## Incident Window
- Date: April 19, 2026
- System: Agentic AI Orchestration (Redis Streams + FastAPI + Claude)
- Impact: Elevated latency and uneven writer throughput under burst load

## 1) Scaling Issue: Hot Partition on Writer Stream

### What happened
Under load, all writer workers competed for heavy write/read activity tied to a single logical output pattern. Even though writer consumers used a consumer group, stream activity became skewed around the same high-traffic key path during peak token emission.

### Why consumer groups helped
- Consumer groups distributed step execution ownership and prevented duplicate processing.
- Pending-entry tracking plus `XAUTOCLAIM` improved resiliency for crashed consumers.

### New issue introduced
- Head-of-line blocking appeared when large writer tasks produced long token streams.
- Smaller, later tasks waited behind long-running stream activity and shared read loops.

### Fix applied
- Implemented **per-task-id stream sharding** (`stream:user_output:{task_id}`).
- Each task now streams to its own key, isolating token flow and reducing contention.
- SSE clients subscribe only to the task-specific stream they requested.

### Result
- Lower p95 stream latency for concurrent tasks.
- Better fairness: small tasks no longer queue behind long writer generations.

## 2) Design Change: Shared `stream:results` Coupling

### Initial design mistake
All agents published into one shared `stream:results`. This made onboarding easy but caused:
- Tight coupling between unrelated result types.
- More parsing conditionals in aggregator logic.
- Harder independent scaling/alerting per agent pipeline.

### Improved design direction
Split result channels by role:
- `stream:results:retriever`
- `stream:results:analyzer`
- (optional) `stream:results:writer`

### Benefits
- Lower schema ambiguity.
- Cleaner ownership boundaries.
- Easier per-agent throughput monitoring and backpressure handling.
- Safer incremental schema evolution per role.

## 3) Trade-Offs

### a) Redis Streams vs Kafka (simplicity vs durability)
- **Choice made:** Redis Streams.
- **Benefit:** Simple deployment, low operational overhead, fast iteration.
- **Cost:** Weaker long-horizon durability/replay model compared to Kafka retention strategies.

### b) Polling Aggregator vs Event-Driven Callbacks (latency vs complexity)
- **Choice made:** Polling with blocking reads (`XREADGROUP` + timeout).
- **Benefit:** Predictable control flow and straightforward worker implementation.
- **Cost:** Slight additional latency and periodic empty wake-ups compared to pure push/event callback architecture.

### c) Single Orchestrator vs Distributed Planning (consistency vs throughput)
- **Choice made:** Single orchestrator planner path.
- **Benefit:** Deterministic task plans, easier dependency correctness, simpler debugging.
- **Cost:** Planning throughput is bounded by one orchestrator instance unless explicitly scaled and coordinated.

## Action Items

1. Introduce per-agent result streams and route-specific aggregators.
2. Add queue depth and lag dashboards per stream/group.
3. Add canary load tests focused on long-writer + short-writer mixed workloads.
4. Add idempotency keys for replay-safe downstream dispatch.
