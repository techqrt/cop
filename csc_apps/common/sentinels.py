class _NotProvided:
    """Marks a field as absent from the request, distinct from an explicit null/clear
    value. Ported from pms_apps/common/sentinels.py (docs/pms-reference-analysis.md §4)."""

    def __repr__(self):
        return "NOT_PROVIDED"

    def __bool__(self):
        return False


NOT_PROVIDED = _NotProvided()
