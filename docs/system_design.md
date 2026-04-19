# Agentic AI System Design

## 1) Architecture Diagram (ASCII)

```text
┌────────────────────┐         POST /task          ┌──────────────────────────┐
│ Client (curl/UI)   │ ───────────────────────────> │ FastAPI API Layer         │
└────────────────────┘                              │ - /task                   │
         ▲                                          │ - /stream/{task_id}       │
         │ SSE                                      │ - /health                 │
         │                                          └──────────────┬───────────┘
         │                                                         │
         │                                                         ▼
         │                                         ┌─────────────────────────────┐
         │                                         │ BatchDispatcher             │
         │                                         │ - window: 2s               │
         │                                         │ - max batch: 5             │
         │                                         └──────────────┬──────────────┘
         │                                                        │
         │                                                        ▼
         │                                         ┌─────────────────────────────┐
         │                                         │ Orchestrator Agent          │
         │                                         │ - task decomposition        │
         │                                         │ - DAG registration          │
         │                                         │ - initial step dispatch     │
         │                                         └──────────────┬──────────────┘
         │                                                        │ XADD step
         │                                                        ▼
         │                                         ┌─────────────────────────────┐
         │                                         │ Redis Stream: stream:tasks  │
         │                                         └───────┬──────────┬──────────┘
         │                                                 │          │
         │                                 group:retriever │          │ group:analyzer
         │                                                 │          │
         │                                                 ▼          ▼
         │                                  ┌────────────────┐   ┌────────────────┐
         │                                  │ Retriever Agent│   │ Analyzer Agent │
         │                                  └───────┬────────┘   └───────┬────────┘
         │                                          │                    │
         │                                          └────────┬───────────┘
         │                                                   ▼
         │                                  ┌──────────────────────────────────┐
         │                                  │ Redis Stream: stream:results     │
         │                                  └────────────────┬─────────────────┘
         │                                                   │ group:aggregator
         │                                                   ▼
         │                                  ┌──────────────────────────────────┐
         │                                  │ ResultAggregator                 │
         │                                  │ - marks steps complete           │
         │                                  │ - unlocks DAG dependencies       │
         │                                  │ - dispatches next steps          │
         │                                  └────────────────┬─────────────────┘
         │                                                   │
         │                                                   ▼
         │                                  ┌──────────────────────────────────┐
         │                                  │ Writer Agent (group:writer)      │
         │                                  │ - Claude streaming generation     │
         │                                  └────────────────┬─────────────────┘
         │                                                   │ token SSE events
         │                                                   ▼
         │                          ┌────────────────────────────────────────────┐
         └────────────────────────── │ Redis Stream: stream:user_output:{task_id} │
                                     └────────────────────────────────────────────┘
```

## 2) Component Descriptions

- **FastAPI API Layer**
  - Accepts user task input and returns a `task_id`.
  - Exposes SSE endpoint for real-time token/status streaming.
  - Exposes health endpoint for Redis and all worker consumers.

- **BatchDispatcher**
  - Buffers incoming task submissions for up to 2 seconds or until 5 tasks arrive.
  - Dispatches each batch to the orchestrator.
  - Preserves independent `task_id` for each item.

- **Orchestrator Agent**
  - Uses Claude (`claude-sonnet-4-20250514`) to decompose user task into JSON steps.
  - Registers DAG in resolver and dispatches only dependency-ready steps.
  - Publishes user-visible status events for planning and dispatch.

- **Retriever Agent**
  - Consumes retriever-assigned tasks from `stream:tasks`.
  - Performs simulated or real web search (Brave API supported).
  - Emits normalized retrieval payload into `stream:results`.

- **Analyzer Agent**
  - Consumes analyzer tasks from `stream:tasks`.
  - Calls Claude (non-streaming) to produce structured analysis JSON.
  - Emits analysis payload into `stream:results`.

- **Writer Agent**
  - Consumes writer tasks from `stream:tasks`.
  - Calls Claude with streaming enabled and publishes token chunks.
  - Writes SSE payloads into `stream:user_output:{task_id}`.

- **ResultAggregator**
  - Consumes `stream:results`.
  - Marks step completion/failure in DAG resolver.
  - Unlocks newly-ready steps and dispatches them.
  - Emits completion/failure terminal events for SSE clients.

- **Redis Streams**
  - Durable queue backbone for task, result, and user-output messages.
  - Consumer groups provide horizontal worker scaling and replay/recovery semantics.

## 3) Data Flow Walkthrough (Sample Task)

Sample task:
`Research the latest trends in quantum computing and write a 3-paragraph executive summary with key takeaways`

1. Client submits `POST /task` with sample prompt.
2. API enqueues request in BatchDispatcher and returns `task_id`.
3. BatchDispatcher flushes a batch (size or 2-second timeout) to orchestrator.
4. Orchestrator decomposes into DAG:
   - Step 1: retriever
   - Step 2: analyzer depends on 1
   - Step 3: writer depends on 2
5. Orchestrator dispatches Step 1 to `stream:tasks`.
6. Retriever consumes Step 1, gathers references, publishes success to `stream:results`.
7. Aggregator marks Step 1 complete, resolves DAG, dispatches Step 2.
8. Analyzer consumes Step 2, synthesizes insights, publishes success to `stream:results`.
9. Aggregator marks Step 2 complete, dispatches Step 3.
10. Writer consumes Step 3, streams tokens to `stream:user_output:{task_id}`.
11. `/stream/{task_id}` endpoint relays each SSE chunk to client in real time.
12. Aggregator receives final writer step result, emits terminal `task_completed`.

## 4) Message Schema Definitions

### `stream:tasks`

```json
{
  "task_id": "uuid",
  "step_id": "1",
  "agent": "retriever|analyzer|writer",
  "instruction": "step instruction",
  "depends_on": "[\"1\"]",
  "context": "{\"1\": {\"documents\": [...]}}",
  "user_task": "original user request",
  "enqueued_at": "ISO-8601 timestamp"
}
```

### `stream:results`

Success:
```json
{
  "task_id": "uuid",
  "step_id": "2",
  "agent": "analyzer",
  "status": "success",
  "output": "{\"summary\":\"...\",\"key_takeaways\":[...]}",
  "processed_at": "ISO-8601 timestamp"
}
```

Failure:
```json
{
  "task_id": "uuid",
  "step_id": "2",
  "agent": "analyzer",
  "status": "failed",
  "reason": "error message",
  "processed_at": "ISO-8601 timestamp"
}
```

### `stream:user_output:{task_id}`

```json
{
  "sse": "data: {\"token\":\"...\",\"task_id\":\"uuid\",\"step\":\"writer\"}\\n\\n",
  "terminal": "false|true",
  "created_at": "ISO-8601 timestamp"
}
```

### `stream:dlq`

```json
{
  "source_stream": "stream:tasks",
  "source_message_id": "1713456-0",
  "reason": "all retries failed",
  "payload": "{\"task_id\":\"...\",\"step_id\":\"...\"}",
  "moved_at": "ISO-8601 timestamp"
}
```

## 5) Scaling Strategy

- **Horizontal worker scaling**
  - Run multiple retriever/analyzer/writer containers.
  - Keep one consumer group per role (`group:retriever`, `group:analyzer`, `group:writer`).
  - Redis automatically partitions work among consumers within each group.

- **Failure recovery**
  - Unacked messages remain in PEL (pending entries list).
  - Other consumers use `XAUTOCLAIM` with 30s idle timeout to take ownership.
  - Failed messages after retry budget are written to `stream:dlq`.

- **Throughput improvements**
  - Increase stream read `count` and run multiple consumers per role.
  - Shard user output stream keys by `task_id` to reduce contention.
  - Add more aggregator instances if result throughput increases.

## 6) Technology Justification: Redis Streams vs Kafka

Why Redis Streams is chosen for this implementation:

- **Operational simplicity**
  - Single Redis service is easy to run in local dev and Docker Compose.
  - Lower setup burden than operating Kafka + ZooKeeper/KRaft + topic management.

- **Strong fit for low-latency orchestration**
  - Consumer groups, pending-entry tracking, and message replay are enough for agent workflows.
  - Fast, in-memory-first behavior benefits real-time SSE token streaming.

- **Cost and iteration speed**
  - Faster developer onboarding and fewer infra moving parts.
  - Good trade for medium-scale workloads where ultra-long retention is not required.

When Kafka would win:
- Very high throughput, strict long-term durability requirements, and multi-datacenter replication at scale.

## 7) Realistic Sample Output (Quantum Computing Task)

```text
Quantum computing has moved beyond a pure “qubit count race” and toward engineering reliability. Across recent deployments, teams are prioritizing error mitigation, calibration stability, and hybrid quantum-classical workflows to generate repeatable outcomes in optimization and simulation. This shift reflects a market transition from research milestones to practical execution, where measurable performance and reproducibility matter more than raw hardware claims.

Commercial maturity is also improving. Platform vendors now provide better software tooling, managed runtimes, and domain libraries that reduce integration friction for enterprise teams. At the same time, buyers are becoming more disciplined in evaluating fit-for-purpose use cases, benchmark transparency, and interoperability. As a result, adoption is progressing through focused pilots instead of broad, speculative rollouts.

Key takeaways: first, anchor quantum initiatives to business problems with clear KPIs and short pilot cycles; second, invest early in internal talent and governance to reduce vendor and execution risk; third, adopt a hybrid roadmap that combines classical optimization with targeted quantum experimentation. Organizations that build capability now are likely to capture outsized value as hardware and algorithms continue to mature.
```
