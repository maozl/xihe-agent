"""Context compression — summarize middle turns when approaching the model limit."""

import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)

SUMMARY_PREFIX = (
    "[CONTEXT COMPACTION] Earlier conversation turns were compacted. "
    "The summary below describes completed work. Continue from where "
    "things left off and avoid repeating work:\n"
)

PRUNED_PLACEHOLDER = "[Old tool output cleared to save context space]"
CHARS_PER_TOKEN = 4


class ContextCompressor:
    """Compress conversation context when approaching the model's context limit."""

    def __init__(
        self,
        context_length: int,
        threshold_percent: float = 0.50,
        aux=None,
    ):
        self.context_length = context_length
        self.threshold_tokens = int(context_length * threshold_percent)
        self._aux = aux
        # Per-session compaction memory: one shared compressor instance serves
        # every chat (SharedContext), so a single previous-summary slot bled
        # session A's summary into session B's compaction prompt. Keyed by
        # session, FIFO-bounded.
        self._summaries: dict[str, str] = {}
        self.compression_count = 0

    def _summary_for(self, key: Optional[str]) -> Optional[str]:
        return self._summaries.get(key or "_default")

    def _remember_summary(self, key: Optional[str], summary: str) -> None:
        k = key or "_default"
        self._summaries[k] = summary
        if len(self._summaries) > 32:
            self._summaries.pop(next(iter(self._summaries)))

    def should_compress(self, messages: list[dict]) -> bool:
        """Quick pre-flight check using rough estimate."""
        rough = self._estimate_tokens(messages)
        return rough >= self.threshold_tokens

    def compress(self, messages: list[dict],
                 session_key: Optional[str] = None) -> list[dict]:
        """Compress by summarizing middle turns.

        ``session_key`` keys the progressive-summary memory — required when
        one compressor instance is shared across sessions (gateway/serve);
        omitted, all callers share the "_default" slot.
        """
        # Need at least head + some middle + 3 tail
        min_needed = 3 + 3 + 1
        if len(messages) <= min_needed:
            return messages

        messages = self._prune_old_tool_results(messages)

        head_end = 3  # system + first exchange
        # Align forward past tool results
        while head_end < len(messages) and messages[head_end].get("role") == "tool":
            head_end += 1

        tail_start = self._find_tail_start(messages, head_end)
        if head_end >= tail_start:
            return messages

        turns_to_summarize = messages[head_end:tail_start]
        if not turns_to_summarize:
            return messages

        logger.info(
            "Compressing: summarizing turns %d-%d (%d turns), "
            "protecting %d head + %d tail",
            head_end, tail_start, len(turns_to_summarize),
            head_end, len(messages) - tail_start,
        )

        summary = self._generate_summary(turns_to_summarize, session_key)

        compressed = list(messages[:head_end])

        if not summary:
            summary = (
                f"{SUMMARY_PREFIX}"
                f"{len(turns_to_summarize)} turns were removed to free context. "
                "Continue based on recent messages and current state."
            )

        # Pick a role that avoids consecutive same-role
        last_head_role = messages[head_end - 1].get("role", "user") if head_end > 0 else "user"
        first_tail_role = messages[tail_start].get("role", "user") if tail_start < len(messages) else "user"
        if last_head_role in ("assistant", "tool"):
            summary_role = "user"
        else:
            summary_role = "assistant"
        if summary_role == first_tail_role:
            summary_role = "assistant" if summary_role == "user" else "user"

        compressed.append({"role": summary_role, "content": summary})
        compressed.extend(messages[tail_start:])

        self.compression_count += 1
        compressed = self._sanitize_tool_pairs(compressed)

        logger.info("Compressed: %d -> %d messages", len(messages), len(compressed))
        return compressed

    # Same cut as the old per-char ord() > 0x2E80 loop, counted by the regex
    # engine — this runs over every message on every estimate call, and the
    # Python loop dominated at long-transcript sizes.
    _CJK_RE = re.compile(r"[⺁-\U0010FFFF]+")

    @classmethod
    def _text_tokens(cls, text: str) -> int:
        """CJK chars tokenize ~1/char on GLM-class models while ASCII runs
        ~4/char; a flat chars/4 undercounts Chinese-heavy prompts 2-3x, which
        let turns blow past the compression threshold."""
        cjk = sum(len(m) for m in cls._CJK_RE.findall(text))
        return cjk + (len(text) - cjk) // CHARS_PER_TOKEN

    @classmethod
    def _estimate_tokens(cls, messages: list[dict]) -> int:
        total = 0
        for msg in messages:
            content = msg.get("content") or ""
            total += cls._text_tokens(content) + 10
            for tc in msg.get("tool_calls") or []:
                if isinstance(tc, dict):
                    args = tc.get("function", {}).get("arguments", "")
                    total += cls._text_tokens(args)
        return total

    @staticmethod
    def _prune_old_tool_results(messages: list[dict]) -> list[dict]:
        """Replace old tool results (>200 chars) with placeholder, protect last 20."""
        result = [m.copy() for m in messages]
        protect_from = max(0, len(result) - 20)
        for i in range(protect_from):
            msg = result[i]
            if msg.get("role") != "tool":
                continue
            content = msg.get("content", "")
            if content and len(content) > 200 and content != PRUNED_PLACEHOLDER:
                result[i] = {**msg, "content": PRUNED_PLACEHOLDER}
        return result

    def _find_tail_start(self, messages: list[dict], head_end: int) -> int:
        """Walk backward to find tail boundary (protect ~20K tokens of recent context)."""
        tail_budget = min(20000, int(self.context_length * 0.20))
        n = len(messages)
        accumulated = 0
        cut_idx = n

        for i in range(n - 1, head_end, -1):
            content = messages[i].get("content") or ""
            msg_tokens = len(content) // CHARS_PER_TOKEN + 10
            if accumulated + msg_tokens > tail_budget * 1.5:
                break
            accumulated += msg_tokens
            cut_idx = i

        # Ensure at least 3 tail messages
        cut_idx = min(cut_idx, n - 3)
        return max(cut_idx, head_end + 1)

    def _generate_summary(self, turns: list[dict],
                          session_key: Optional[str] = None) -> Optional[str]:
        """Use auxiliary client to generate structured summary of middle turns."""
        if not self._aux:
            return None

        previous = self._summary_for(session_key)
        content = self._serialize_turns(turns)
        if previous:
            prompt = (
                "Update the context compaction summary below by incorporating new turns.\n\n"
                f"PREVIOUS SUMMARY:\n{previous}\n\n"
                f"NEW TURNS:\n{content}\n\n"
                "Use this structure:\n"
                "## Goal\n## Progress (Done / In Progress / Blocked)\n"
                "## Key Decisions\n## Relevant Files\n## Next Steps\n"
                "Be specific — include file paths, commands, error messages."
            )
        else:
            prompt = (
                "Create a structured handoff summary for a later assistant.\n\n"
                f"TURNS TO SUMMARIZE:\n{content}\n\n"
                "Use this structure:\n"
                "## Goal\n## Progress (Done / In Progress / Blocked)\n"
                "## Key Decisions\n## Relevant Files\n## Next Steps\n"
                "Be specific — include file paths, commands, error messages. "
                "Target ~2000 tokens."
            )

        try:
            response = self._aux.call_llm(
                task="compression",
                messages=[{"role": "user", "content": prompt}],
                max_tokens=4000,
            )
            if not response:
                return None
            from core.auxiliary_client import extract_content_or_reasoning
            summary = extract_content_or_reasoning(response)
            self._remember_summary(session_key, summary)
            return f"{SUMMARY_PREFIX}{summary}"
        except Exception as e:
            logger.warning("Summary generation failed: %s", e)
            return None

    @staticmethod
    def _serialize_turns(turns: list[dict]) -> str:
        parts = []
        for msg in turns:
            role = msg.get("role", "unknown")
            content = msg.get("content") or ""
            if len(content) > 4000:
                content = content[:3000] + "\n...[truncated]...\n" + content[-1000:]

            if role == "tool":
                parts.append(f"[TOOL RESULT]: {content}")
            elif role == "assistant":
                tc_str = ""
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        fn = tc.get("function", {})
                        name = fn.get("name", "?")
                        args = fn.get("arguments", "")[:1000]
                        tc_str += f"\n  {name}({args})"
                parts.append(f"[ASSISTANT]: {content}{tc_str}")
            else:
                parts.append(f"[{role.upper()}]: {content}")
        return "\n\n".join(parts)

    @staticmethod
    def _sanitize_tool_pairs(messages: list[dict]) -> list[dict]:
        """Fix orphaned tool_call / tool_result pairs after compression."""
        surviving_ids = set()
        for msg in messages:
            if msg.get("role") == "assistant":
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        cid = tc.get("id", "")
                        if cid:
                            surviving_ids.add(cid)

        result_ids = set()
        for msg in messages:
            if msg.get("role") == "tool":
                cid = msg.get("tool_call_id")
                if cid:
                    result_ids.add(cid)

        orphaned = result_ids - surviving_ids
        if orphaned:
            messages = [
                m for m in messages
                if not (m.get("role") == "tool" and m.get("tool_call_id") in orphaned)
            ]

        missing = surviving_ids - result_ids
        if missing:
            patched = []
            for msg in messages:
                patched.append(msg)
                if msg.get("role") == "assistant":
                    for tc in msg.get("tool_calls") or []:
                        cid = tc.get("id") if isinstance(tc, dict) else ""
                        if cid in missing:
                            patched.append({
                                "role": "tool",
                                "content": "[Result from earlier conversation — see summary above]",
                                "tool_call_id": cid,
                            })
            messages = patched

        return messages
