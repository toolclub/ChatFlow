"""
Agent Harness
Wires all 9 layers together and exposes a single façade used by main.py.

Layer map (OS analogy):
  1. Prompt      → layers.prompt        (personality / templates)
  2. Capability  → layers.capability    (tools: models, embeddings)
  3. Memory      → layers.memory        (data structures: Message, Conversation)
  4. Runtime     → layers.runtime       (agent loop: stream / sync)
  5. State       → layers.state         (working memory: in-process store)
  6. Context     → layers.context       (message assembly + compression trigger)
  7. Persistence → layers.persistence   (disk checkpoint: save/load/delete)
  8. Verification→ layers.verification  (logging / observability)
  9. Extension   → layers.extension     (applied in main.py: CORS, plugins)
"""
from typing import Optional, AsyncGenerator

from config import SUMMARY_MODEL
from layers.memory import Conversation, Message
from layers.state import StateManager
from layers import context, persistence, prompt, verification
from layers.runtime import stream as _runtime_stream, call_sync


class AgentHarness:
    def __init__(self):
        # Layer 5 – State: working memory
        self.state = StateManager()
        # Layer 7 – Persistence: restore checkpoints from disk on startup
        self.state.load_from(persistence.load_all())

    # ── Conversation CRUD ──────────────────────────────────────────────────

    def create_conversation(
        self,
        conv_id: str,
        title: str = "新对话",
        system_prompt: str = "",
    ) -> Conversation:
        conv = Conversation(
            id=conv_id,
            title=title,
            system_prompt=prompt.ensure_system_prompt(system_prompt),  # Layer 1
        )
        self.state.set(conv)           # Layer 5
        persistence.save(conv)         # Layer 7
        return conv

    def get_conversation(self, conv_id: str) -> Optional[Conversation]:
        return self.state.get(conv_id)  # Layer 5

    def list_conversations(self) -> list[dict]:
        return sorted(
            [
                {"id": c.id, "title": c.title, "updated_at": c.updated_at}
                for c in self.state.all()
            ],
            key=lambda x: x["updated_at"],
            reverse=True,
        )

    def delete_conversation(self, conv_id: str) -> None:
        self.state.remove(conv_id)      # Layer 5
        persistence.delete(conv_id)     # Layer 7

    def add_message(self, conv_id: str, role: str, content: str) -> None:
        conv = self.state.get(conv_id)
        if not conv:
            return
        conv.messages.append(Message(role=role, content=content))  # Layer 3
        if conv.title == "新对话" and role == "user":
            conv.title = content[:30] + ("..." if len(content) > 30 else "")
        persistence.save(conv)          # Layer 7

    def _save(self, conv: Conversation) -> None:
        """Compatibility shim – direct persistence save (used by PATCH endpoint)."""
        persistence.save(conv)          # Layer 7

    # ── Context assembly (Layer 6) ─────────────────────────────────────────

    def build_messages(self, conv: Conversation) -> list[dict]:
        return context.build_messages(conv)

    # ── Runtime: streaming chat (Layer 4) ─────────────────────────────────

    async def chat_stream(
        self,
        model: str,
        messages: list[dict],
        temperature: float = 0.7,
    ) -> AsyncGenerator[str, None]:
        verification.log_chat("stream", model)  # Layer 8
        async for chunk in _runtime_stream(model=model, messages=messages, temperature=temperature):
            yield chunk

    # ── Context compression (Layer 6 + 4 + 3 + 1) ─────────────────────────

    async def maybe_compress(self, conv_id: str) -> bool:
        conv = self.state.get(conv_id)
        if not conv or not context.should_compress(conv):   # Layer 6
            return False

        # Layer 6 – Context: identify new messages to summarise (cursor → window start)
        to_summarise, new_cursor = context.slice_for_compression(conv)
        if not to_summarise:
            return False

        # Layer 1 – Prompt: build summary template
        history_text = "\n".join(
            f"{'用户' if m.role == 'user' else 'AI'}: {m.content}"
            for m in to_summarise
        )
        compress_messages = prompt.build_summary_messages(history_text, conv.mid_term_summary)

        # Layer 4 – Runtime: call summary model synchronously
        new_summary = await call_sync(
            model=SUMMARY_MODEL,
            messages=compress_messages,
            temperature=0.2,
        )

        # Layer 3 – Memory: update summary and advance cursor; messages untouched
        conv.mid_term_summary = new_summary.strip()
        conv.mid_term_cursor = new_cursor

        # Layer 7 – Persistence: checkpoint
        persistence.save(conv)

        # Layer 8 – Verification: log
        window_count = len(conv.messages) - new_cursor
        verification.log_compression(
            conv_id, len(to_summarise), window_count, len(conv.mid_term_summary)
        )
        return True


# Global singleton
harness = AgentHarness()
