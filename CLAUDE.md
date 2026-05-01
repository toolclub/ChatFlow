# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

ChatFlow is an AI conversation platform built with LangChain + LangGraph. Core architecture: **DB-driven + State machines + Model-agnostic + Full-chain streaming**.

## Build and Test Commands

### Docker (recommended for production)
```bash
cd llm-chat
docker compose up -d            # Start all services
docker compose logs -f backend  # View logs
docker compose down             # Stop
docker compose up -d --build    # Rebuild after code changes
```

### Local Development
```bash
# Backend (Python 3.12+)
cd llm-chat/backend
python -m venv venv && source venv/bin/activate
pip install -e ..               # Install from pyproject.toml
python main.py                  # Runs on :8000

# Frontend (Node 18+)
cd llm-chat/frontend
npm install && npm run dev      # Runs on :5173
npm run build                   # Production build
```

### Testing
```bash
cd llm-chat/backend
pytest tests/ -v                # All tests
pytest tests/test_fsm.py -v     # Single module
pytest tests/ -m unit           # Unit tests (no DB/Redis)
pytest tests/ -m integration    # Integration tests
pytest tests/ -m smoke          # End-to-end smoke tests
```

Test documentation: `backend/test_case.md` - read this file only, test code is in `tests/`.

## Architecture

### Backend Structure
- `graph/agent.py` - LangGraph graph construction and compilation
- `graph/state.py` - GraphState with step_results field
- `graph/edges.py` - Conditional routing logic
- `graph/nodes/` - Individual node implementations (BaseNode inheritance)
- `graph/runner/stream.py` - SSE driver (queue + heartbeat + resume)
- `fsm/` - State machines (conversation, tool execution, plan step, SSE events)
- `memory/` - Short/medium/long-term memory management
- `db/` - SQLAlchemy ORM, Redis shared state, migrations
- `tools/` - Built-in tools, sandbox execution, MCP protocol

### Key Flow
User message → semantic_cache_check → vision_node (images) → route_model (intent routing) → retrieve_context (RAG) → planner (multi-step plans) → call_model → ToolNode → call_model_after_tool → reflector → save_response → extract_memory → compress_memory

### State Machines (fsm/)
- Conversation: ACTIVE → STREAMING → COMPLETED/ERROR
- Tool execution: RUNNING → DONE/ERROR/TIMEOUT
- Plan step: PENDING → RUNNING → DONE/FAILED
- SSE events: Priority-based event type registry

## Critical Rules (from spec.md - violations cause bugs)

1. **Never infer state from model output text** - Use DB fields (tool_executions.status, clarification_data)
2. **Never embed structured data in content** - Use separate fields (tool_summary, step_summary)
3. **Never hardcode SSE event types** - Use fsm/sse_events.py SSEEventType enum
4. **Never truncate message list** - History window controlled by context_builder; AIMessage↔ToolMessage pairs must be complete
5. **Never await in sync functions** - All DB queries must be in async def
6. **All LLM calls must be streaming** - Use _stream_tokens/_stream_tokens_with_tools, never ainvoke
7. **State changes via state machine** - StreamSession._conv_sm.send_event() → persist to DB
8. **Never modify plan list in-place** - _mark_step() returns new list
9. **Never swallow exceptions** - except Exception: pass must at least logger.warning

## Adding New Features

| What to add | Files to modify |
|-------------|-----------------|
| New DB field | db/models.py + db/migrate.py + memory/schema.py |
| New state | fsm/*.py enum + transitions |
| New SSE event | fsm/sse_events.py enum + _PRIORITY_ORDER |
| New tool | tools/builtin/ @tool function + tools/__init__.py registration |
| New graph node | Inherit BaseNode + graph/agent.py registration + edges.py routing |

After any code change: run corresponding module tests per test_case.md "spec对照检查" table.

## Configuration

All config via `.env` file (copy from llm-chat/.env example). Model switching only requires changing:
```env
LLM_BASE_URL="https://..."
CHAT_MODEL="model-name"
```

No code changes needed. Models must support OpenAI-compatible protocol + function calling.

## COMPAT Layer

Code marked `# COMPAT:` handles model-specific behaviors (MiniMax residual text, ` QHBoxLayout` tags). These no-op for other models - switching models won't break.

## Structured Thinking Protocol

All model calls push thinking process as structured segments via SSE:
- Key: (node, step_index, phase) tuple
- Nodes: planner, route_model, call_model, call_model_after_tool, reflector, vision
- Never parse thinking text for decisions - only for display/persistence