from __future__ import annotations

from app.models.term import Term, VerificationResult


class TranslationVerifier:
    def verify(self, source_text: str, normalized_text: str, zh_text: str, candidate_terms: list[Term]) -> VerificationResult:
        actions: list[str] = []
        fixed = zh_text.strip() or normalized_text.strip() or source_text.strip()
        missing: list[Term] = []
        combined_source = f"{source_text}\n{normalized_text}".lower()

        for term in sorted(candidate_terms, key=lambda item: item.priority, reverse=True):
            for forbidden in term.forbidden_translations:
                if forbidden and forbidden in fixed:
                    fixed = fixed.replace(forbidden, term.zh_cn)
                    actions.append(f"forbidden_replaced:{term.id}")

            source_hit = any(phrase and phrase.lower() in combined_source for phrase in term.match_phrases)
            if source_hit and term.zh_cn not in fixed:
                missing.append(term)
                if fixed:
                    fixed = f"{term.zh_cn}：{fixed}"
                else:
                    fixed = term.zh_cn
                actions.append(f"required_term_inserted:{term.id}")
            elif source_hit:
                actions.append(f"required_term_ok:{term.id}")

        if not actions:
            actions.append("no_term_rule_triggered")
        return VerificationResult(text=fixed, actions=actions, missing_terms=missing)

