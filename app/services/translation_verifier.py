from __future__ import annotations

from app.models.term import Term, VerificationResult


class TranslationVerifier:
    def verify(
        self,
        source_text: str,
        normalized_text: str,
        zh_text: str,
        candidate_terms: list[Term],
    ) -> VerificationResult:
        actions: list[str] = []
        fixed = zh_text.strip() or normalized_text.strip() or source_text.strip()
        missing: list[Term] = []
        combined_source = f"{source_text}\n{normalized_text}".lower()

        if contains_untranslated_foreign_script(fixed):
            fixed = "（翻译未返回中文）"
            actions.append("non_chinese_translation_replaced")

        for term in sorted(candidate_terms, key=lambda item: item.priority, reverse=True):
            for forbidden in term.forbidden_translations:
                if forbidden and forbidden in fixed:
                    fixed = fixed.replace(forbidden, term.zh_cn)
                    actions.append(f"forbidden_replaced:{term.id}")

            source_hit = any(phrase and phrase.lower() in combined_source for phrase in term.match_phrases)
            if source_hit and term.zh_cn not in fixed:
                missing.append(term)
                fixed = f"{term.zh_cn}: {fixed}" if fixed else term.zh_cn
                actions.append(f"required_term_inserted:{term.id}")
            elif source_hit:
                actions.append(f"required_term_ok:{term.id}")

        if not actions:
            actions.append("no_term_rule_triggered")
        return VerificationResult(text=fixed, actions=actions, missing_terms=missing)


def contains_untranslated_foreign_script(text: str) -> bool:
    if not text.strip():
        return False
    foreign_count = 0
    latin_count = 0
    han_count = 0
    for char in text:
        code = ord(char)
        if "\u4e00" <= char <= "\u9fff":
            han_count += 1
        if ("a" <= char.lower() <= "z") or char.isdigit():
            latin_count += 1
        if (
            0x3040 <= code <= 0x30FF  # Hiragana and Katakana
            or 0xAC00 <= code <= 0xD7AF  # Hangul syllables
            or 0x1100 <= code <= 0x11FF  # Hangul Jamo
            or 0x0E00 <= code <= 0x0E7F  # Thai
        ):
            foreign_count += 1
    return foreign_count >= 2 or (han_count == 0 and latin_count >= 4)
