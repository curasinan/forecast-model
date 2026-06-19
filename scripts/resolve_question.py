"""Record a question's resolution (YES=1 or NO=0).

Usage:
    python scripts/resolve_question.py                    # Interactive mode
    python scripts/resolve_question.py <id> <YES|NO>      # Direct mode
    python scripts/resolve_question.py --list              # Show all questions & status

Examples:
    python scripts/resolve_question.py openai-consumer-hardware-2026 NO
    python scripts/resolve_question.py --list
"""

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.agent.scheduler import load_questions
from src.config import settings
from src.evaluation.tracker import EvaluationTracker


def get_tracker():
    return EvaluationTracker(settings.database_path)


def list_questions(tracker: EvaluationTracker):
    """Show all questions with resolution status."""
    questions = load_questions()

    # Get existing resolutions
    import sqlite3
    resolutions = {}
    try:
        with sqlite3.connect(settings.database_path) as conn:
            rows = conn.execute("SELECT question_id, outcome, resolved_at FROM resolutions").fetchall()
            for row in rows:
                resolutions[row[0]] = {"outcome": row[1], "resolved_at": row[2]}
    except Exception:
        pass

    now = datetime.now()
    print(f"\n{'ID':<45s} {'Status':<12s} {'Deadline':<12s} {'Days':<6s} Title")
    print("-" * 130)

    for q in sorted(questions, key=lambda x: x.deadline):
        days = (q.deadline - now).days
        if q.id in resolutions:
            r = resolutions[q.id]
            outcome = "YES" if r["outcome"] == 1 else "NO"
            status = f"RESOLVED:{outcome}"
        elif days < 0:
            status = "EXPIRED"
        else:
            status = "OPEN"

        print(f"{q.id:<45s} {status:<12s} {q.deadline.strftime('%Y-%m-%d'):<12s} {days:>4d}d  {q.title[:50]}")

    # Summary
    resolved_count = len(resolutions)
    open_count = len(questions) - resolved_count
    print(f"\nTotal: {len(questions)} | Open: {open_count} | Resolved: {resolved_count}")


def resolve_question(tracker: EvaluationTracker, question_id: str, outcome_str: str):
    """Record a resolution."""
    questions = load_questions()
    q = next((q for q in questions if q.id == question_id), None)

    if not q:
        print(f"ERROR: Question '{question_id}' not found.")
        print("Available IDs:")
        for q in questions:
            print(f"  {q.id}")
        return False

    outcome_str = outcome_str.upper().strip()
    if outcome_str in ("YES", "1", "TRUE"):
        outcome = 1
    elif outcome_str in ("NO", "0", "FALSE"):
        outcome = 0
    else:
        print(f"ERROR: Invalid outcome '{outcome_str}'. Use YES or NO.")
        return False

    # Confirm
    outcome_word = "YES" if outcome == 1 else "NO"
    print(f"\nQuestion: {q.title}")
    print(f"Outcome:  {outcome_word}")
    print(f"ID:       {q.id}")

    tracker.store_resolution(question_id=q.id, outcome=outcome)
    print(f"\nResolution recorded: {q.id} -> {outcome_word}")
    return True


def interactive_mode(tracker: EvaluationTracker):
    """Interactive resolution recording."""
    print("\n=== Question Resolution Recorder ===\n")
    list_questions(tracker)

    print("\nEnter question ID to resolve (or 'q' to quit):")
    question_id = input("> ").strip()
    if question_id.lower() == "q":
        return

    print("Outcome (YES or NO):")
    outcome = input("> ").strip()

    resolve_question(tracker, question_id, outcome)


def main():
    tracker = get_tracker()

    if len(sys.argv) == 1:
        interactive_mode(tracker)
    elif sys.argv[1] == "--list":
        list_questions(tracker)
    elif len(sys.argv) >= 3:
        resolve_question(tracker, sys.argv[1], sys.argv[2])
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
