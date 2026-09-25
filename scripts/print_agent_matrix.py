"""Print the capability matrix of the three masters, and where they differ.

Each divergence is either reviewed as by-design (ADR-001) or listed as
unreviewed with the rationale it still needs. Read-only; no model calls.

Run:  .venv/bin/python scripts/print_agent_matrix.py
"""

from yuyutsava.core.agent_profiles import (
    REVIEWED_DIVERGENCES,
    divergences,
    format_matrix,
    unreviewed_divergences,
)


def main() -> None:
    print(format_matrix())
    all_div, unreviewed = divergences(), unreviewed_divergences()
    print(
        f"\n{len(all_div)} capabilities differ across the three masters — "
        f"{len(all_div) - len(unreviewed)} by design, {len(unreviewed)} unreviewed:\n"
    )
    for cap, row in all_div.items():
        note = REVIEWED_DIVERGENCES.get(cap)
        mark = "  by-design" if (note and note.by_design) else "UNREVIEWED"
        lack = ",".join(r for r, v in row.items() if not v)
        print(f"  [{mark}] {cap:34} lacks={lack}")
    if unreviewed:
        print("\nUnreviewed divergences need a decision, not necessarily a change:")
        for cap in unreviewed:
            note = REVIEWED_DIVERGENCES.get(cap)
            print(f"\n  {cap}\n    {note.rationale if note else 'no rationale recorded'}")


if __name__ == "__main__":
    main()
