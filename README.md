# 🤖 Agentic AI System for Multi-Step Tasks

A production-grade multi-agent orchestration system that decomposes complex user tasks into dependency-ordered execution steps, coordinates specialized agents through Redis Streams, and streams the final output in real time via Server-Sent Events.

## Architecture

```
┌────────────────────┐         POST /task          ┌──────────────────────────┐
│ Client (curl/UI)   │ ──────────────────────────► │ FastAPI API Layer         │
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
         │                                         │ - task decomposition (DAG)  │
         │                                         │ - dependency resolution     │
         │                                         │ - step dispatch             │
         │                                         └──────────────┬──────────────┘
         │                                                        │ XADD step
         │                                                        ▼
         │                                         ┌─────────────────────────────┐
         │                                         │ Redis Stream: stream:tasks  │
         │                                         └───────┬──────────┬──────────┘
         │                                                 │          │
         │                                 group:retriever │          │ group:analyzer
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
         └───────────────────────── │ Redis Stream: stream:user_output:{task_id} │
                                    └────────────────────────────────────────────┘
```

## Tech Stack

| Component | Technology |
|---|---|
| API Framework | FastAPI + Uvicorn |
| Message Queue | Redis Streams (consumer groups) |
| LLM Provider | Anthropic Claude (claude-sonnet-4-20250514) |
| HTTP Client | httpx (async) |
| Validation | Pydantic v2 |
| Infrastructure | Docker Compose |
| Testing | pytest + pytest-asyncio |
| Logging | Structured JSON logging |

## Project Structure

```
agentic_system/
├── agents/
│   ├── base_agent.py          # Abstract base — consumer loop, DLQ, health
│   ├── orchestrator.py        # Task decomposition, DAG dispatch
│   ├── retriever.py           # Web search (Brave API) or simulation
│   ├── analyzer.py            # Structured analysis via Claude
│   └── writer.py              # Streaming token generation
├── pipeline/
│   ├── queue.py               # Async Redis Streams client
│   ├── dag_resolver.py        # In-memory DAG dependency tracker
│   ├── result_aggregator.py   # Step completion → next step dispatch
│   └── batch_dispatcher.py    # Manual time/size bounded batching
├── api/
│   ├── routes.py              # POST /task, GET /stream, GET /health
│   └── sse.py                 # SSE formatting and stream generator
├── utils/
│   ├── logger.py              # JSON structured logging
│   └── retry.py               # @async_retry with exponential backoff
├── tests/
│   ├── test_orchestrator.py   # Fallback plan & dispatch tests
│   ├── test_batch_dispatcher.py # Batch size & time window tests
│   └── test_retry.py          # Retry success & exhaustion tests
├── docs/
│   ├── system_design.md       # Full system design document
│   └── post_mortem.md         # Scaling issues & trade-offs
├── main.py                    # FastAPI app factory & service wiring
├── config.py                  # Pydantic settings (env-based)
├── requirements.txt
├── Dockerfile
└── docker-compose.yml
```

## Quick Start

### Prerequisites

- Python 3.11+
- Redis 7+ (or Docker)

### Option 1: Docker Compose (Recommended)

```bash
# Clone the repository
git clone https://github.com/swarajladke/Agentic-AI-System.git
cd Agentic-AI-System

# Optional: set your Anthropic API key (system works without it via simulation mode)
export ANTHROPIC_API_KEY=your-key-here

# Start Redis + App
docker-compose up --build
```

### Option 2: Local Development

```bash
# Start Redis
docker run -d -p 6379:6379 redis:7.4-alpine

# Install dependencies
pip install -r requirements.txt

# Run the server
uvicorn main:app --host 0.0.0.0 --port 8000
```

### Submit a Task

```bash
# Submit a task
curl -X POST http://localhost:8000/task \
  -H "Content-Type: application/json" \
  -d '{"task": "Research the latest trends in quantum computing and write a 3-paragraph executive summary"}'

# Response:
# {"task_id": "uuid", "status": "queued", "stream_url": "/stream/uuid"}

# Stream the output in real time
curl -N http://localhost:8000/stream/{task_id}
```

Or open `http://localhost:8000` in a browser for the built-in UI.

### Run Tests

```bash
pytest tests/ -v
```

## How It Works

### 1. Task Submission & Batching

User submits a natural-language task via `POST /task`. The **BatchDispatcher** collects tasks for up to 2 seconds or until 5 tasks arrive, then flushes the batch to the orchestrator.

### 2. Task Decomposition (DAG)

The **Orchestrator Agent** calls Claude to decompose the task into a JSON execution plan — a DAG of steps with dependency links. Each step is assigned to a specialized agent. If no API key is configured, a deterministic 3-step fallback plan is used.

### 3. Agent Execution

Three specialized agents consume from Redis Streams:

- **Retriever** — Gathers documents via Brave Search API or simulation
- **Analyzer** — Produces structured insights (trends, risks, takeaways) via Claude
- **Writer** — Generates the final output using Claude's streaming API, relaying tokens in real time

### 4. DAG Resolution & Chaining

The **ResultAggregator** consumes agent outputs from `stream:results`, marks steps as complete in the **DagResolver**, identifies newly-unblocked steps, and dispatches them. This continues until all steps are done.

### 5. Real-Time Streaming

The Writer publishes token chunks to `stream:user_output:{task_id}`. The SSE endpoint `GET /stream/{task_id}` tails this stream and yields each token to the client in real time.

## Failure Handling

The system implements three layers of failure defense:

| Layer | Mechanism | Purpose |
|---|---|---|
| **Retry** | `@async_retry` with exponential backoff | Handles transient API failures |
| **Consumer Recovery** | `XAUTOCLAIM` (30s idle timeout) | Reclaims messages from crashed workers |
| **Dead Letter Queue** | `stream:dlq` with full context | Captures permanently failed messages |

## Scaling Strategy

- **Horizontal scaling** — Run multiple agent containers per role; Redis consumer groups auto-distribute work
- **Stream sharding** — Per-task output streams (`stream:user_output:{task_id}`) prevent head-of-line blocking
- **Failure recovery** — Pending entry list (PEL) + XAUTOCLAIM ensures no message loss

## Key Design Decisions

| Decision | Choice | Trade-off |
|---|---|---|
| Message Queue | Redis Streams over Kafka | Simpler ops, lower latency; weaker long-term durability |
| Aggregation | Polling (XREADGROUP) over event callbacks | Simpler control flow; slight extra latency |
| Planning | Single orchestrator over distributed | Deterministic plans, easier debugging; bounded throughput |
| Framework | No LangChain/CrewAI — hand-written agents | Full control, no black-box behavior; more code to maintain |

## Configuration

All settings are loaded from environment variables (or `.env` file):

| Variable | Default | Description |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis connection URL |
| `ANTHROPIC_API_KEY` | `None` | Anthropic API key (optional — simulation mode if absent) |
| `ANTHROPIC_MODEL` | `claude-sonnet-4-20250514` | Claude model to use |
| `BATCH_SIZE` | `5` | Max tasks per batch |
| `BATCH_WINDOW_SECONDS` | `2.0` | Max wait before flushing batch |
| `STREAM_BLOCK_MS` | `3000` | Redis blocking read timeout |
| `CLAIM_IDLE_MS` | `30000` | XAUTOCLAIM idle threshold |

## Documentation

- [System Design Document](docs/system_design.md) — Architecture, data flow, message schemas, scaling strategy
- [Post-Mortem](docs/post_mortem.md) — Scaling issues, design changes, and trade-offs

## License

MIT
