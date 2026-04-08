from examples.manager import get_examples_for_rag
from config.settings import RAG_TOP_K, RAG_SOURCE


def _score_similarity(example_indicators: dict, current_indicators: dict, example_direction: str = None, current_bias: str = None) -> float:
    """
    Compute a similarity score between two indicator snapshots.
    Higher = more similar. Score is in [0, 1].
    """
    score = 0.0
    weights = 0.0

    ex_trend = (example_indicators.get("trend") or {})
    cur_trend = (current_indicators.get("trend") or {})

    # 1. Overall trend match (most important)
    if ex_trend.get("overall_trend") and cur_trend.get("overall_trend"):
        if ex_trend["overall_trend"] == cur_trend["overall_trend"]:
            score += 0.30
        weights += 0.30

    # 2. Weekly trend match
    if ex_trend.get("weekly_trend") and cur_trend.get("weekly_trend"):
        if ex_trend["weekly_trend"] == cur_trend["weekly_trend"]:
            score += 0.20
        weights += 0.20

    # 3. Daily trend match
    if ex_trend.get("daily_trend") and cur_trend.get("daily_trend"):
        if ex_trend["daily_trend"] == cur_trend["daily_trend"]:
            score += 0.15
        weights += 0.15

    # 4. Trade direction matches current bias
    if example_direction and current_bias:
        ex_dir_bias = "bullish" if example_direction == "long" else "bearish"
        if ex_dir_bias == current_bias:
            score += 0.20
        weights += 0.20

    # 5. Funding rate sign match
    ex_fr = (example_indicators.get("funding_rate") or {}).get("funding_rate")
    cur_fr = (current_indicators.get("funding_rate") or {}).get("funding_rate")
    if ex_fr is not None and cur_fr is not None:
        if (ex_fr >= 0) == (cur_fr >= 0):
            score += 0.10
        weights += 0.10

    # 6. Long/short ratio direction match
    ex_ls = (example_indicators.get("long_short_ratio") or {}).get("long_ratio")
    cur_ls = (current_indicators.get("long_short_ratio") or {}).get("long_ratio")
    if ex_ls is not None and cur_ls is not None:
        if (ex_ls > 0.5) == (cur_ls > 0.5):
            score += 0.05
        weights += 0.05

    return score / weights if weights > 0 else 0.0


def retrieve_similar_examples(
    current_indicators: dict,
    asset: str = None,
    top_k: int = RAG_TOP_K,
    current_bias: str = None,
    rag_source: str = RAG_SOURCE,
) -> list[dict]:
    """
    Retrieve the most similar historical examples based on market indicators.
    Searches across all assets — ticker-agnostic similarity matching.
    Returns top_k examples sorted by similarity (highest first).
    rag_source: None = all examples, 'manual' = hand-added, 'auto' = teacher-generated.
    Defaults to RAG_SOURCE from settings.py.
    """
    examples = get_examples_for_rag(source=rag_source)
    if not examples:
        return []

    scored = []
    for ex in examples:
        ind = ex.get("indicators") or {}
        similarity = _score_similarity(ind, current_indicators, ex.get("direction"), current_bias)
        scored.append((similarity, ex))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [ex for score, ex in scored[:top_k] if score >= 0.6]


def format_examples_for_prompt(examples: list[dict]) -> str:
    """
    Format retrieved examples into a readable string for the LLM prompt.
    """
    if not examples:
        return "No similar historical examples found."

    lines = []
    for i, ex in enumerate(examples, 1):
        ind = ex.get("indicators") or {}
        trend = (ind.get("trend") or {}).get("overall_trend", "unknown")
        lines.append(
            f"Example #{i} | {ex['asset']} | {ex['trade_date']} | {ex['direction'].upper()}\n"
            f"  Entry: {ex['entry1']}"
            + (f" / {ex['entry2']}" if ex.get("entry2") else "")
            + f" | SL: {ex['sl']} | TP1: {ex['tp1']}"
            + (f" | TP2: {ex['tp2']}" if ex.get("tp2") else "")
            + f"\n  Market trend at time: {trend}"
            + (f"\n  Notes: {ex['notes']}" if ex.get("notes") else "")
        )

    return "\n\n".join(lines)
