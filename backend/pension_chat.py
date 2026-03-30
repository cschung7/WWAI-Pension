"""Pension Chat — streaming LLM with live registry context injection.

POST /api/pension/chat   → SSE stream
GET  /api/pension/chat/conversations
GET  /api/pension/chat/conversations/{id}/messages
DELETE /api/pension/chat/conversations/{id}
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import time
import uuid
from pathlib import Path
from typing import AsyncIterator

import httpx
from fastapi import APIRouter, Request
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from backend.pension_registry import _fetch_pension_runs, TIER_META, PRODUCT_TYPE_META

logger = logging.getLogger(__name__)
router = APIRouter()

# ─── DB for conversation persistence ──────────────────────────────────────────
CONV_DB = Path(__file__).parent.parent / "data" / "pension_chat.db"
CONV_DB.parent.mkdir(parents=True, exist_ok=True)


def _conv_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(CONV_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS conversations (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at REAL,
            updated_at REAL
        )""")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            conversation_id TEXT,
            role TEXT,
            content TEXT,
            created_at REAL
        )""")
    conn.commit()
    return conn


# ─── Registry context builder ─────────────────────────────────────────────────
def _build_registry_context() -> str:
    """Summarise top pension runs for system-prompt injection."""
    try:
        runs = _fetch_pension_runs(fsc_only=True)
    except Exception:
        return ""

    if not runs:
        return ""

    lines = ["## 현재 연금 전략 레지스트리 (FSC 규정 충족, Calmar 순)"]
    lines.append(f"총 {len(runs)}개 전략 등록됨\n")

    # Best per tier + product_type
    best: dict[str, dict] = {}
    for r in runs:
        key = f"{r['tier']}_{r['product_type']}"
        if key not in best or r["calmar"] > best[key]["calmar"]:
            best[key] = r

    lines.append("### 티어·유형별 대표 전략")
    lines.append(f"{'전략':<44} {'유형':<14} {'CAGR':>6}  {'DD':>7}  {'SR':>5}  {'Tier'}")
    lines.append("-" * 90)
    for r in sorted(best.values(), key=lambda x: x["cagr"], reverse=True)[:12]:
        lines.append(
            f"{r['strategy_label']:<44} {r['product_type_label']:<14} "
            f"{r['cagr']:>5.1f}%  {r['maxdd']:>6.2f}%  {r['sharpe']:>5.3f}  "
            f"{r['tier']} {r['tier_label']}"
        )

    lines.append("\n### 티어 기준")
    for tier, meta in TIER_META.items():
        lines.append(
            f"- **{tier} {meta['label']}**: CAGR ≥ {meta['min_cagr']}%, "
            f"MaxDD > {meta['max_dd']}%"
        )

    lines.append("\n### 상품 유형")
    for pt, meta in PRODUCT_TYPE_META.items():
        lines.append(f"- **{meta['label']} ({pt})**: {meta['desc']}")

    return "\n".join(lines)


# ─── System prompt ─────────────────────────────────────────────────────────────
_SYSTEM_PROMPT_TEMPLATE = """당신은 WWAI 연금 어드바이저입니다. 한국 IRP/DC 퇴직연금 투자자의 질문에 친절하고 명확하게 답변합니다.

## 역할과 톤
- 투자 전문가처럼 설명하되, 금융 용어는 쉽게 풀어서 설명
- 항상 근거(데이터, 규정)를 제시
- 구체적인 수치 예시를 들어 설명
- 지나치게 길지 않게, 핵심만 짚기

## 한국 연금 제도 지식
- **IRP (개인형 퇴직연금)**: 연간 최대 900만원 세액공제 대상. 위험자산 ≤ 70% 규정 적용
- **DC (확정기여형)**: 회사가 운영. 마찬가지로 위험자산 ≤ 70%
- **DB (확정급여형)**: 회사가 운용. 본인이 ETF 선택 불가
- **FSC 위험자산 규정**: IRP/DC에서 주식형 ETF 등 위험자산은 총 자산의 70% 이내
- **세액공제**: IRP 연 900만원 한도, 근로소득 5,500만원 이하 시 16.5% 공제

## 상품 유형 설명
- **포트폴리오형**: Top-5 ETF를 주간 레짐 신호로 선정 + 채권 슬리브 → 전문 운용
- **단일ETF 혼합형**: ETF 1개 + 채권 30% → IRP에 직접 편입 가능한 단순 구조
- **자체충족형**: ETF 내부에 이미 채권 포함 (예: SOL 미국배당미국채혼합50) → 혼자서도 FSC 충족

## 현재 연금 레지스트리 데이터
{registry_context}

## 안내 사항
- 연금 투자 상담 시 반드시 "과거 수익이 미래를 보장하지 않습니다" 고지
- 개인별 상황(나이, 은퇴 시기, 위험 성향)에 따라 적합한 전략이 다름
- 구체적인 투자 결정은 전문 FP/FP 상담 권고
- 더 자세한 프로필 분석은 /fund-buyer-profile 링크 안내
"""


def _build_system_prompt() -> str:
    ctx = _build_registry_context()
    return _SYSTEM_PROMPT_TEMPLATE.format(registry_context=ctx)


# ─── OpenRouter streaming call ────────────────────────────────────────────────
ENV_PATH = Path("/mnt/nas/gpt/.env")


def _load_api_key() -> str:
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if line.startswith("OPENROUTER_API_KEY="):
                return line.split("=", 1)[1].strip().strip('"')
    import os
    return os.getenv("OPENROUTER_API_KEY", "")


async def _stream_llm(
    messages: list[dict],
) -> AsyncIterator[str]:
    api_key = _load_api_key()
    if not api_key:
        yield "API 키가 설정되지 않았습니다."
        return

    payload = {
        "model": "google/gemini-2.0-flash-001",
        "messages": messages,
        "stream": True,
        "temperature": 0.4,
        "max_tokens": 1800,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        async with client.stream(
            "POST",
            "https://openrouter.ai/api/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        ) as resp:
            async for raw in resp.aiter_lines():
                if not raw.startswith("data:"):
                    continue
                chunk = raw[5:].strip()
                if chunk == "[DONE]":
                    break
                try:
                    d = json.loads(chunk)
                    delta = d["choices"][0]["delta"].get("content", "")
                    if delta:
                        yield delta
                except Exception:
                    pass


# ─── Routes ───────────────────────────────────────────────────────────────────
class PensionChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None


@router.post("/chat")
async def pension_chat(req: PensionChatRequest):
    conversation_id = req.conversation_id or uuid.uuid4().hex[:12]
    conn = _conv_db()

    # Ensure conversation row exists
    existing = conn.execute(
        "SELECT id FROM conversations WHERE id=?", (conversation_id,)
    ).fetchone()
    now = time.time()
    if not existing:
        title = req.message[:50]
        conn.execute(
            "INSERT INTO conversations (id,title,created_at,updated_at) VALUES (?,?,?,?)",
            (conversation_id, title, now, now),
        )
        conn.commit()

    # Save user message
    conn.execute(
        "INSERT INTO messages (conversation_id,role,content,created_at) VALUES (?,?,?,?)",
        (conversation_id, "user", req.message, now),
    )
    conn.commit()

    # Build history (last 10 turns)
    rows = conn.execute(
        "SELECT role,content FROM messages WHERE conversation_id=? ORDER BY created_at LIMIT 20",
        (conversation_id,),
    ).fetchall()
    conn.close()

    history = [{"role": r, "content": c} for r, c in rows]
    # Remove the current user message from history (already appended below)
    if history and history[-1]["role"] == "user":
        history = history[:-1]

    system_prompt = _build_system_prompt()
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": req.message})

    async def event_gen():
        full_answer = []
        try:
            yield {"event": "start", "data": json.dumps({"conversation_id": conversation_id})}
            async for chunk in _stream_llm(messages):
                full_answer.append(chunk)
                yield {
                    "event": "chunk",
                    "data": json.dumps({"text": chunk}, ensure_ascii=False),
                }
        except Exception as e:
            logger.error(f"Pension chat error: {e}", exc_info=True)
            yield {"event": "error", "data": json.dumps({"text": str(e)})}
        finally:
            answer = "".join(full_answer)
            if answer:
                c = _conv_db()
                c.execute(
                    "INSERT INTO messages (conversation_id,role,content,created_at) VALUES (?,?,?,?)",
                    (conversation_id, "assistant", answer, time.time()),
                )
                c.execute(
                    "UPDATE conversations SET updated_at=? WHERE id=?",
                    (time.time(), conversation_id),
                )
                c.commit()
                c.close()
            yield {
                "event": "done",
                "data": json.dumps(
                    {"conversation_id": conversation_id}, ensure_ascii=False
                ),
            }

    return EventSourceResponse(
        event_gen(),
        headers={
            "Content-Encoding": "identity",
            "X-Accel-Buffering": "no",
            "Cache-Control": "no-cache",
        },
    )


@router.get("/conversations")
async def list_conversations(q: str = ""):
    conn = _conv_db()
    if q:
        rows = conn.execute(
            """SELECT c.id, c.title, c.updated_at
               FROM conversations c
               WHERE c.title LIKE ?
               ORDER BY c.updated_at DESC LIMIT 50""",
            (f"%{q}%",),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT id, title, updated_at FROM conversations ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    conn.close()
    return [{"id": r[0], "title": r[1], "updated_at": r[2]} for r in rows]


@router.get("/conversations/{conv_id}/messages")
async def get_messages(conv_id: str):
    conn = _conv_db()
    rows = conn.execute(
        "SELECT role, content, created_at FROM messages WHERE conversation_id=? ORDER BY created_at",
        (conv_id,),
    ).fetchall()
    conn.close()
    return [{"role": r[0], "content": r[1], "created_at": r[2]} for r in rows]


@router.delete("/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    conn = _conv_db()
    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conv_id,))
    conn.execute("DELETE FROM conversations WHERE id=?", (conv_id,))
    conn.commit()
    conn.close()
    return {"ok": True}
