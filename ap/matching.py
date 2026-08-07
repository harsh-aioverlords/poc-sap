"""Invoice-to-PO line resolution.

Item codes genuinely disagree across the client's documents — Motion bills
``15179`` / ``V3.0730-56`` against PO material ``10313140`` / ``P-V3073056`` —
and the vendor's own printed PO line reference can be wrong (Motion prints
``X00020`` on a line that is really PO line 00030). So resolution runs a
four-tier cascade and then solves a one-to-one assignment, rather than trusting
any single signal.
"""

from __future__ import annotations

import difflib
import re
from itertools import permutations

from .models import PO, Invoice, InvoiceLine, LineMatch, POLine

# Tier weights: how much a signal contributes to the pairing score.
_W_PO_REF = 0.35
_W_CODE = 0.90
_W_QTY_PRICE = 1.00
_W_DESC = 0.55

_MIN_SCORE = 0.30


def normalize_code(code: str) -> str:
    """Strip punctuation and Jindal's ``P-`` material prefix for comparison.

    ``P-V3073056`` -> ``V3073056``, ``V3.0730-56`` -> ``V3073056``,
    ``P-438MN2`` -> ``438MN2``. This single normalization is what lets the
    Motion and Grainger invoices resolve against PO material codes.
    """
    if not code:
        return ""
    # Drop the P- prefix before removing punctuation, so the dash disambiguates
    # a real prefix from a code that merely happens to start with P.
    stripped = re.sub(r"^\s*P-", "", str(code).strip(), flags=re.IGNORECASE)
    return re.sub(r"[^A-Za-z0-9]", "", stripped).upper()


def code_variants(*codes: str) -> set[str]:
    out: set[str] = set()
    for code in codes:
        norm = normalize_code(code)
        if norm:
            out.add(norm)
            # Also index the trailing alphanumeric run, so V3073056 matches
            # a PO code that embeds it with a different prefix.
            tail = re.sub(r"^[A-Z]+", "", norm)
            if len(tail) >= 5:
                out.add(tail)
    return out


def confidence(score: float) -> float:
    """Map a raw pairing score onto 0..1.

    One decisive signal (exact quantity + unit price, weight 1.0) already means
    high confidence; corroborating signals push it towards certainty. Saturating
    rather than dividing by a fixed ceiling keeps a lone strong match from
    reading as a coin flip in the UI.
    """
    return round(min(1.0, 1.0 - pow(2.718281828, -1.6 * max(score, 0.0))), 3)


def _desc_ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return difflib.SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


def score_pair(inv_line: InvoiceLine, po_line: POLine) -> tuple[float, str, str]:
    """Score one (invoice line, PO line) pair.

    Returns (score, tier, human-readable reason). Tiers are evaluated
    independently and combined, so a wrong PO-line reference cannot by itself
    outweigh exact quantity+price agreement.
    """
    signals: list[tuple[float, str, str]] = []

    # Tier 1 — vendor-printed PO line reference (weak; must be corroborated).
    ref = re.sub(r"[^0-9]", "", inv_line.po_line_ref or "")
    if ref:
        if ref.zfill(5) == po_line.line_no.replace(".", "") or ref == po_line.line_no.lstrip("0"):
            signals.append((_W_PO_REF, "po_line_ref", f"vendor printed PO line ref {inv_line.po_line_ref}"))

    # Tier 2 — normalized material code.
    inv_codes = code_variants(inv_line.vendor_item_code, inv_line.description)
    po_codes = code_variants(po_line.material_code, po_line.description)
    shared = inv_codes & po_codes
    if shared:
        signals.append(
            (_W_CODE, "material_code", f"material code matches after normalization ({sorted(shared)[0]})")
        )

    # Tier 3 — quantity and unit price agreement (highest precision in practice).
    qty_ok = po_line.qty > 0 and abs(inv_line.qty - po_line.qty) < 0.001
    price_ok = po_line.net_price > 0 and abs(inv_line.unit_price - po_line.net_price) < 0.01
    if qty_ok and price_ok:
        signals.append(
            (_W_QTY_PRICE, "qty_price", f"qty {inv_line.qty:g} and unit price {inv_line.unit_price:,.2f} both match")
        )
    elif price_ok:
        signals.append((_W_QTY_PRICE * 0.6, "qty_price", f"unit price {inv_line.unit_price:,.2f} matches"))
    elif qty_ok and abs(inv_line.amount - po_line.total) < 0.01:
        signals.append((_W_QTY_PRICE * 0.8, "qty_price", "quantity and line amount match"))

    # Tier 4 — fuzzy description.
    ratio = _desc_ratio(inv_line.description, po_line.description)
    if ratio >= 0.6:
        signals.append((_W_DESC * ratio, "description", f"description similarity {ratio:.0%}"))

    if not signals:
        return 0.0, "unmatched", "no signal"

    total = sum(w for w, _, _ in signals)
    best_tier = max(signals, key=lambda s: s[0])[1]
    reason = "; ".join(r for _, _, r in sorted(signals, key=lambda s: -s[0]))
    return total, best_tier, reason


def _best_assignment(
    inv_lines: list[InvoiceLine], po_lines: list[POLine]
) -> dict[int, tuple[int, float, str, str]]:
    """One-to-one assignment maximising total score.

    Greedy misassigns when two PO lines share a price, so with the small line
    counts here (<=9) we search permutations exactly.
    """
    if not inv_lines or not po_lines:
        return {}

    grid: list[list[tuple[float, str, str]]] = [
        [score_pair(inv, po) for po in po_lines] for inv in inv_lines
    ]

    n_inv, n_po = len(inv_lines), len(po_lines)
    best_total, best_map = -1.0, {}

    # Assign each invoice line to a distinct PO line (or to nothing).
    if n_inv <= 8 and n_po <= 9:
        for combo in permutations(range(n_po), min(n_inv, n_po)):
            total = 0.0
            mapping: dict[int, tuple[int, float, str, str]] = {}
            for i, j in enumerate(combo):
                score, tier, reason = grid[i][j]
                if score >= _MIN_SCORE:
                    total += score
                    mapping[i] = (j, score, tier, reason)
            if total > best_total:
                best_total, best_map = total, mapping
        return best_map

    # Fallback for unusually wide documents: greedy by descending score.
    taken: set[int] = set()
    order = sorted(
        ((grid[i][j][0], i, j) for i in range(n_inv) for j in range(n_po)), reverse=True
    )
    for score, i, j in order:
        if score < _MIN_SCORE or i in best_map or j in taken:
            continue
        s, tier, reason = grid[i][j]
        best_map[i] = (j, s, tier, reason)
        taken.add(j)
    return best_map


def match_lines(invoice: Invoice, po: PO) -> list[LineMatch]:
    """Resolve every invoice goods line to a PO line."""
    candidates = [l for l in po.postable_lines() if not l.delivery_cost]
    if not candidates:
        candidates = po.postable_lines()

    assignment = _best_assignment(invoice.lines, candidates)
    matches: list[LineMatch] = []

    for i, inv_line in enumerate(invoice.lines):
        hit = assignment.get(i)
        if hit is None:
            matches.append(
                LineMatch(
                    invoice_line_ref=inv_line.line_ref or str(i + 1),
                    tier="unmatched",
                    confidence=0.0,
                    reason="no PO line agreed on code, quantity, price or description",
                    invoice_qty=inv_line.qty,
                    invoice_amount=inv_line.amount,
                )
            )
            continue

        j, score, tier, reason = hit
        po_line = candidates[j]
        matches.append(
            LineMatch(
                invoice_line_ref=inv_line.line_ref or str(i + 1),
                po_line_no=po_line.line_no,
                tier=tier,  # type: ignore[arg-type]
                confidence=confidence(score),
                reason=reason,
                invoice_qty=inv_line.qty,
                invoice_amount=inv_line.amount,
                po_qty=po_line.qty,
                po_amount=po_line.total,
            )
        )

    return matches


def match_charge_to_line(description: str, po: PO) -> POLine | None:
    """Find an open PO delivery-cost line a charge can post against (planned cost)."""
    best: tuple[float, POLine] | None = None
    for line in po.postable_lines():
        if not line.delivery_cost or line.amount_open <= 0.005:
            continue
        ratio = _desc_ratio(description, line.description)
        if ratio >= 0.5 and (best is None or ratio > best[0]):
            best = (ratio, line)
    return best[1] if best else None
