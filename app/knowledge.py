"""
Knowledge base module for the AI Voice Agent.

Loads a JSON knowledge base and provides search functionality
for answering user questions about the company.
"""

import json
import os
from typing import List, Dict
from app.utils.logger import logger


KB_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "knowledge", "knowledge.json")

# Cached knowledge base data
_knowledge_data: List[Dict] = []


def load_knowledge_base() -> List[Dict]:
    """
    Load the knowledge base from knowledge.json.

    Returns:
        List of knowledge base entries
    """
    global _knowledge_data

    if _knowledge_data:
        return _knowledge_data

    try:
        with open(KB_PATH, "r", encoding="utf-8") as f:
            _knowledge_data = json.load(f)
        logger.info(f"📚 Knowledge base loaded: {len(_knowledge_data)} entries from {KB_PATH}")
    except FileNotFoundError:
        logger.error(f"❌ Knowledge base file not found: {KB_PATH}")
        _knowledge_data = []
    except json.JSONDecodeError as e:
        logger.error(f"❌ Invalid JSON in knowledge base: {e}")
        _knowledge_data = []

    return _knowledge_data


def search_knowledge(query: str, max_results: int = 3) -> List[Dict]:
    """
    Search the knowledge base for entries matching the query.

    Uses keyword-based matching: checks if any keywords from a KB entry
    appear in the query (case-insensitive).

    Args:
        query: The search query from the user
        max_results: Maximum number of results to return

    Returns:
        List of matching knowledge base entries with question/answer pairs
    """
    kb = load_knowledge_base()
    if not kb:
        return []

    query_lower = query.lower().strip()
    query_words = set(query_lower.split())

    scored_results = []

    for entry in kb:
        score = 0

        keywords = [kw.lower() for kw in entry.get("keywords", [])]
        for keyword in keywords:
            if keyword in query_lower:
                score += 2
            elif any(keyword in word for word in query_words):
                score += 1

        # Check question similarity
        question_lower = entry.get("question", "").lower()
        question_words = set(question_lower.split())
        common_words = query_words & question_words
        # Exclude common stop words from scoring
        stop_words = {"what", "is", "the", "a", "an", "how", "do", "does", "can", "i", "you", "your", "are", "about", "of", "to", "in", "for", "and", "or"}
        meaningful_common = common_words - stop_words
        score += len(meaningful_common)

        if score > 0:
            scored_results.append({
                "question": entry.get("question", ""),
                "answer": entry.get("answer", ""),
                "category": entry.get("category", "General"),
                "score": score,
            })

    scored_results.sort(key=lambda x: x["score"], reverse=True)

    results = [
        {"question": r["question"], "answer": r["answer"], "category": r["category"]}
        for r in scored_results[:max_results]
    ]

    logger.info(f"🔍 KB Search | Query: '{query}' | Found: {len(results)} results")

    return results
