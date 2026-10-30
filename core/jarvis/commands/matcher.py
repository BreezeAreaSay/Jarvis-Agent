"""Сопоставление фразы с командами.

Фраза нормализуется, из неё убираются обращение и слова-паразиты («ну», «давай», «пожалуйста»). Шаблоны без
слотов сравниваются целиком (точно, по основам слов и нечётко — rapidfuzz по заранее подготовленным строкам),
шаблоны со слотами — выравниванием по словам: слова шаблона сравниваются нечётко, слот забирает часть фразы
({number} — число, {app} — программа, {site} — сайт, {desk} — стол, {text}/{query} — свободный хвост,
{keys} — клавиши словами, {when} — время напоминания словами: «завтра в 9», jarvis.commands.when). Фраза
из нескольких команд («сверни все окна и включи музыку») делится на части, только если каждая часть — команда
со «Связывать»; без запятых и «и» («включи музыку открой вс код») — ещё и перед повелительным глаголом, если
целиком фраза ни на что не похожа.

Лишние слова снижают похожесть, поэтому вопросы и длинные просьбы уходят к ИИ.
"""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from functools import lru_cache
from itertools import pairwise
from typing import Any

from rapidfuzz import fuzz, process

from jarvis.commands import keys as keyops
from jarvis.commands import numbers
from jarvis.commands.model import Entry
from jarvis.commands.textutil import (
    ANYWHERE_FILLERS,
    LEAD_FILLERS,
    QUESTION_WORDS,
    normalize,
    stem_text,
    strip_fillers,
    word_sim,
    word_threshold,
)
from jarvis.services import CommandMatch, MatchedPart

THRESHOLD = 82.0  # не ниже — команда; ниже — фраза уходит ИИ
QUESTION_THRESHOLD = 90.0  # фраза начинается с вопросительного слова — нужна почти точная команда
LONG_WORDS = 7  # длинная фраза без свободного слота — тоже только почти точное совпадение
LONG_THRESHOLD = 92.0
EARLY_ACCEPT = 96.0  # целая фраза совпала так хорошо, что делить её на части незачем
SOFT_SKIP_SIM = 60.0  # пропущенный предлог шаблона («громкость 50» ↔ «громкость на {number}»)
# шаблон со свободным хвостом подходит почти к чему угодно — при выборе он уступает фразе без слотов,
# даже чуть менее похожей («какая температура в спальне» умного дома важнее «какая температура в {text}»)
FREE_SLOT_PENALTY = 5.0
MAX_PARTS = 6

SLOT_RE = re.compile(r"\{(\w+)\}")
KNOWN_SLOTS = frozenset({"number", "app", "site", "desk", "text", "query", "keys", "when"})
FREE_SLOTS = frozenset({"text", "query"})
NAMED_SLOTS = frozenset({"app", "site", "desk"})
SOFT_WORDS = frozenset(
    {"на", "в", "во", "с", "со", "до", "по", "к", "ко", "о", "об", "у", "за", "из", "для", "мне"}
)

_DECIMAL_COMMA = re.compile(r"(\d),(\d)")
_SPLIT = re.compile(
    r"\s*[,;]\s*|\s+(?:и потом|а потом|и затем|а затем|после этого|после чего|потом|затем|и)\s+",
    re.IGNORECASE,
)
# без запятых (распознавание на ПК их не ставит) части цепочки начинаются с повелительного глагола:
# «включи музыку | открой вс код | и запусти фигму»
_VERB_SPLIT = re.compile(
    r"\s+(?=(?:открой|запусти|включи|выключи|закрой|сверни|разверни|сделай|поставь|покажи|перейди|переключи|"
    r"убавь|прибавь|увеличь|уменьши|выдели|скопируй|вставь|удали|сохрани|найди)(?!\w))",
    re.IGNORECASE,
)

# запрос поиска («найди {query}») не забирает составное дело: «найди отели и забронируй самый дешёвый»,
# «найди файл и отправь его» — это задача агенту, а не поиск в браузере
_TASK_TAIL = re.compile(
    r"(?:^|\s)(?:и|а|потом|затем|после)\s+(?:перешли|отправь|пошли|скинь|напиши|позвони|удали|сохрани|"
    r"забронируй|купи|закажи|оплати|переведи|открой|запусти|включи|выключи|закрой|скачай|установи|"
    r"прочитай|сравни|выбери|запиши|добавь)(?!\w)"
)

# слот → (значения слотов, похожесть 0..100) или None
SlotResolver = Callable[[str], tuple[dict[str, Any], float] | None]


@dataclass(frozen=True)
class Token:
    kind: str  # lit | slot
    value: str  # слово или имя слота
    soft: bool = False  # предлог, который распознавание может «проглотить»


@dataclass(frozen=True)
class Template:
    text: str  # нормализованный шаблон: «громкость на {number}»
    tokens: tuple[Token, ...]
    slots: tuple[str, ...]
    literal_len: int  # сумма длин слов шаблона — чем больше, тем конкретнее шаблон
    words: tuple[str, ...]  # слова шаблона без слотов


def _strip_template(tokens: list[Token]) -> list[Token]:
    """Те же слова-паразиты, что убираются из фразы человека, убираются и из шаблона («сделай громче»)."""
    out = [t for t in tokens if not (t.kind == "lit" and t.value in ANYWHERE_FILLERS)]
    changed = True
    while changed:
        changed = False
        for filler in LEAD_FILLERS:
            n = len(filler)
            head = out[:n]
            if len(out) > n and all(t.kind == "lit" for t in head) and tuple(t.value for t in head) == filler:
                out = out[n:]
                changed = True
                break
    return out or tokens


@lru_cache(maxsize=60_000)
def compile_template(phrase: str) -> Template | None:
    """Фраза-шаблон → Template; пустая фраза — None. Незнакомый слот {x} считается текстом."""
    parts = SLOT_RE.split(phrase)
    tokens: list[Token] = []
    for i, part in enumerate(parts):
        if i % 2:
            name = part if part in KNOWN_SLOTS else "text"
            if tokens and tokens[-1].kind == "slot" and "when" not in (name, tokens[-1].value):
                continue  # два слота подряд не разделить — оставляем первый (время {when} знает свои границы)
            tokens.append(Token("slot", name))
        else:
            tokens.extend(Token("lit", w, w in SOFT_WORDS) for w in normalize(part).split())
    tokens = _strip_template(tokens)
    if not tokens:
        return None
    text = " ".join(t.value if t.kind == "lit" else "{" + t.value + "}" for t in tokens)
    words = tuple(t.value for t in tokens if t.kind == "lit")
    slots = tuple(t.value for t in tokens if t.kind == "slot")
    return Template(text, tuple(tokens), slots, sum(len(w) for w in words), words)


class Index:
    """Подготовленные шаблоны включённых команд: строки для rapidfuzz и шаблоны со слотами по первому
    слову."""

    def __init__(self, entries: list[Entry]) -> None:
        self.entries = entries
        self.lit_texts: list[str] = []
        self.lit_sorted: list[str] = []
        self.lit_stems: list[str] = []
        self.lit_owner: list[int] = []
        self.lit_tpl: list[Template] = []
        self.exact: dict[str, list[int]] = {}
        self.stem_exact: dict[str, list[int]] = {}
        self.slot_tpls: list[tuple[int, Template]] = []
        self.by_first: dict[str, list[int]] = {}
        self.anywhere: list[int] = []
        self.optional: dict[str, set[int]] = {}
        seen: set[tuple[int, str]] = set()
        for ei, entry in enumerate(entries):
            for word in entry.command.optional:
                for w in normalize(word).split():
                    self.optional.setdefault(w, set()).add(ei)
            for phrase in entry.command.phrases:
                tpl = compile_template(phrase)
                if tpl is None or (ei, tpl.text) in seen:
                    continue
                seen.add((ei, tpl.text))
                if tpl.slots:
                    self._add_slot(ei, tpl)
                else:
                    self._add_literal(ei, tpl)
        self.first_words = list(self.by_first)

    def _add_literal(self, ei: int, tpl: Template) -> None:
        i = len(self.lit_texts)
        stems = stem_text(tpl.words)
        self.lit_texts.append(tpl.text)
        self.lit_sorted.append(" ".join(sorted(tpl.words)))
        self.lit_stems.append(stems)
        self.lit_owner.append(ei)
        self.lit_tpl.append(tpl)
        self.exact.setdefault(tpl.text, []).append(i)
        self.stem_exact.setdefault(stems, []).append(i)

    def _add_slot(self, ei: int, tpl: Template) -> None:
        si = len(self.slot_tpls)
        self.slot_tpls.append((ei, tpl))
        first = tpl.tokens[0]
        if first.kind == "slot":
            self.anywhere.append(si)
            return
        self.by_first.setdefault(first.value, []).append(si)
        if first.soft and len(tpl.tokens) > 1 and tpl.tokens[1].kind == "lit":
            self.by_first.setdefault(tpl.tokens[1].value, []).append(si)


@dataclass
class Candidate:
    entry_idx: int
    score: float
    specificity: int
    slots: dict[str, Any] = field(default_factory=dict)
    free_slot: bool = False
    text: str = ""
    lead: str = ""  # первое слово шаблона — глагол для «закрой хром и телеграм»
    literal: bool = False  # шаблон без слотов

    def rank(self, priority: int) -> tuple[float, int, int, int]:
        """Порядок выбора: похожесть (свободный хвост — со штрафом), фраза без слотов, источник,
        конкретность."""
        score = self.score - FREE_SLOT_PENALTY if self.free_slot else self.score
        return (round(score, 1), int(self.literal), priority, self.specificity)


def display_title(entry: Entry, slots: dict[str, Any]) -> str:
    """Название для истории и тоста: «Громкость на 50», «Открыть программу: Spotify»."""
    name = entry.command.name
    number = slots.get("number")
    if number is not None and re.search(r"\bN\b", name):
        return re.sub(r"\bN\b", numbers.fmt_number(float(number)), name, count=1)
    for key in ("app", "site", "desk", "query", "text"):
        value = slots.get(key)
        if value:
            text = str(value)
            return f"{name}: {text if len(text) <= 40 else text[:39] + '…'}"
    return name


class Matcher:
    def __init__(self, resolvers: dict[str, SlotResolver], wake_words: Callable[[], frozenset[str]]) -> None:
        self.resolvers = resolvers
        self.wake_words = wake_words

    # --- вход ---------------------------------------------------------------------------------------
    def match(self, index: Index, text: str) -> CommandMatch | None:
        raw = _DECIMAL_COMMA.sub(r"\1.\2", text)
        whole_text = normalize(raw)
        if not whole_text or not index.entries:
            return None
        wake = self.wake_words()
        whole = self.match_words(index, strip_fillers(normalize(raw).split(), wake))
        if whole is not None and whole.score >= EARLY_ACCEPT and not whole.free_slot:
            return CommandMatch(whole_text, [self._part(index, whole)])
        pieces = [p for p in _SPLIT.split(raw) if p and p.strip()]
        if 2 <= len(pieces) <= MAX_PARTS:
            parts = self._compound(index, pieces, wake)
            if parts:
                return CommandMatch(whole_text, [self._part(index, c) for c in parts])
        if whole is not None:
            return CommandMatch(whole_text, [self._part(index, whole)])
        by_verbs = [q for p in pieces for q in _VERB_SPLIT.split(p) if q and q.strip()]
        if len(pieces) < len(by_verbs) <= MAX_PARTS:
            parts = self._compound(index, by_verbs, wake)
            if parts:
                return CommandMatch(whole_text, [self._part(index, c) for c in parts])
        return None

    def _compound(self, index: Index, pieces: list[str], wake: frozenset[str]) -> list[Candidate] | None:
        parts: list[Candidate] = []
        lead = ""
        for piece in pieces:
            words = normalize(piece).split()
            stripped = strip_fillers(words, wake)
            if _only_fillers(words, wake):
                continue
            cand = self.match_words(index, stripped)
            if cand is None and lead:
                cand = self.match_words(index, [lead, *stripped])
            if cand is None or not index.entries[cand.entry_idx].command.chain:
                return None
            parts.append(cand)
            lead = "" if cand.free_slot else cand.lead
        return parts if len(parts) >= 2 else None

    def _part(self, index: Index, cand: Candidate) -> MatchedPart:
        entry = index.entries[cand.entry_idx]
        return MatchedPart(
            entry.id, display_title(entry, cand.slots), cand.text, round(cand.score, 1), cand.slots
        )

    def match_words(self, index: Index, words: list[str]) -> Candidate | None:
        """Слова одной команды → лучший кандидат не ниже порога или None."""
        if not words:
            return None
        cands: list[Candidate] = []
        self._literal(index, words, cands, None)
        self._slotted(index, words, cands, None)
        present = [w for w in words if w in index.optional]
        if present:
            allowed: set[int] = set().union(*(index.optional[w] for w in present))
            reduced = [w for w in words if w not in index.optional]
            if reduced:
                self._literal(index, reduced, cands, allowed)
                self._slotted(index, reduced, cands, allowed)
        return self._choose(index, words, cands)

    # --- выбор --------------------------------------------------------------------------------------
    def _choose(self, index: Index, words: list[str], cands: list[Candidate]) -> Candidate | None:
        need = QUESTION_THRESHOLD if words[0] in QUESTION_WORDS else THRESHOLD
        long_phrase = len(words) >= LONG_WORDS
        best: Candidate | None = None
        best_key: tuple[float, int, int, int] | None = None
        for cand in cands:
            entry = index.entries[cand.entry_idx]
            if entry.exclude and any(w.startswith(ex) for w in words for ex in entry.exclude):
                continue
            limit = need if cand.score < 100 else 0.0
            if long_phrase and not cand.free_slot and cand.score < 100:
                limit = max(limit, LONG_THRESHOLD)
            if cand.score < limit:
                continue
            key = cand.rank(entry.priority)
            if best_key is None or key > best_key:
                best, best_key = cand, key
        return best

    # --- шаблоны без слотов -------------------------------------------------------------------------
    def _literal(
        self, index: Index, words: list[str], out: list[Candidate], allowed: set[int] | None
    ) -> None:
        if not index.lit_texts:
            return
        query = " ".join(words)
        hits: dict[int, float] = {}
        for i in index.exact.get(query, ()):
            hits[i] = 100.0
        stems = stem_text(words)
        for i in index.stem_exact.get(stems, ()):
            hits[i] = max(hits.get(i, 0.0), 97.0)
        fuzzy: set[int] = set()
        if not hits:
            for choices, q, penalty in (
                (index.lit_texts, query, 0.0),
                (index.lit_sorted, " ".join(sorted(words)), 4.0),
                (index.lit_stems, stems, 3.0),
            ):
                for _, score, i in process.extract(q, choices, scorer=fuzz.ratio, limit=12, score_cutoff=72):
                    value = float(score) - penalty
                    if value > hits.get(i, 0.0):
                        hits[i] = value
                        fuzzy.add(i)
        for i, score in hits.items():
            ei = index.lit_owner[i]
            if allowed is not None and ei not in allowed:
                continue
            tpl = index.lit_tpl[i]
            if i in fuzzy and not _words_cover(tpl.words, words):
                continue
            out.append(
                Candidate(
                    ei, score, tpl.literal_len, {}, False, query, tpl.words[0] if tpl.words else "", True
                )
            )

    # --- шаблоны со слотами -------------------------------------------------------------------------
    def _slotted(
        self, index: Index, words: list[str], out: list[Candidate], allowed: set[int] | None
    ) -> None:
        if not index.slot_tpls:
            return
        ids: set[int] = set(index.anywhere)
        for w in words[:2]:
            for first, _, _ in process.extract(
                w, index.first_words, scorer=fuzz.ratio, score_cutoff=60, limit=None
            ):
                if word_sim(first, w) >= word_threshold(first):
                    ids.update(index.by_first[first])
        cache: dict[tuple[str, int, int], tuple[dict[str, Any], float] | None] = {}
        query = " ".join(words)
        for si in ids:
            ei, tpl = index.slot_tpls[si]
            if allowed is not None and ei not in allowed:
                continue
            found = self._align(tpl, words, cache)
            if found is None:
                continue
            score, slots = found
            lead = tpl.tokens[0].value if tpl.tokens[0].kind == "lit" else ""
            out.append(
                Candidate(ei, score, tpl.literal_len, slots, bool(FREE_SLOTS & set(tpl.slots)), query, lead)
            )

    def _align(
        self,
        tpl: Template,
        words: list[str],
        cache: dict[tuple[str, int, int], tuple[dict[str, Any], float] | None],
    ) -> tuple[float, dict[str, Any]] | None:
        """Лучшее выравнивание фразы по шаблону: (похожесть, слоты) или None."""
        tokens = tpl.tokens
        n_t, n_w = len(tokens), len(words)
        lit_total = float(tpl.literal_len)
        best: list[tuple[float, dict[str, Any]]] = []

        def rec(ti: int, wi: int, acc: float, slot_min: float, slots: dict[str, Any]) -> None:
            if ti == n_t:
                if wi == n_w:
                    lit = acc / lit_total if lit_total else 100.0
                    score = min(lit, slot_min)
                    if not best or score > best[0][0]:
                        best[:] = [(score, slots)]
                return
            tok = tokens[ti]
            if tok.kind == "lit":
                size = len(tok.value)
                if wi < n_w:
                    sim = word_sim(tok.value, words[wi])
                    if sim >= word_threshold(tok.value):
                        rec(ti + 1, wi + 1, acc + sim * size, slot_min, slots)
                    if wi + 1 < n_w:  # «ю туб» ↔ «ютуб»
                        joined = fuzz.ratio(tok.value, words[wi] + words[wi + 1])
                        if joined >= 88:
                            rec(ti + 1, wi + 2, acc + (joined - 4) * size, slot_min, slots)
                nxt = tokens[ti + 1] if ti + 1 < n_t else None
                if tok.soft and not (nxt is not None and nxt.kind == "slot" and nxt.value in FREE_SLOTS):
                    rec(ti + 1, wi, acc + SOFT_SKIP_SIM * size, slot_min, slots)
                return
            for end, values, score in self._spans(tok.value, words, wi, ti == n_t - 1, cache):
                rec(ti + 1, end, acc, min(slot_min, score), {**slots, **values})

        rec(0, 0, 0.0, 100.0, {})
        return best[0] if best else None

    def _spans(
        self,
        name: str,
        words: list[str],
        wi: int,
        last: bool,
        cache: dict[tuple[str, int, int], tuple[dict[str, Any], float] | None],
    ) -> Iterator[tuple[int, dict[str, Any], float]]:
        """Куда может закончиться слот, начатый со слова wi: (конец, значения, похожесть)."""
        n = len(words)
        if wi >= n:
            return
        if name == "number":
            for value, end in numbers.parse_at(words, wi):
                if not last or end == n:
                    yield end, {"number": int(value) if float(value).is_integer() else value}, 100.0
            return
        if name in FREE_SLOTS:
            for end in [n] if last else range(wi + 1, n):
                value = " ".join(words[wi:end])
                if name == "query" and _TASK_TAIL.search(value):
                    continue
                yield end, {name: value}, 100.0
            return
        if name == "keys":
            for end in [n] if last else range(wi + 1, min(n, wi + 6) + 1):
                combo = keyops.parse_spoken(" ".join(words[wi:end]))
                if combo:
                    yield end, {"keys": combo}, 100.0
            return
        resolver = self.resolvers.get(name)
        if resolver is None:
            return
        ends = ([n] if n - wi <= 6 else []) if last else list(range(wi + 1, min(n - 1, wi + 5) + 1))
        if name == "when":
            ends.reverse()  # «завтра в 9 позвонить» — время целиком («завтра в 9»), а не «завтра»
        for end in ends:
            key = (name, wi, end)
            if key not in cache:
                cache[key] = resolver(" ".join(words[wi:end]))
            found = cache[key]
            if found is not None:
                yield end, found[0], found[1]


def _only_fillers(words: list[str], wake: frozenset[str]) -> bool:
    """Часть фразы — только «ну», «давай», «пожалуйста» или обращение."""
    rest = [w for w in words if w not in ANYWHERE_FILLERS and w not in wake]
    for filler in LEAD_FILLERS:
        if tuple(rest) == filler:
            return True
    return not rest


def _words_cover(template_words: tuple[str, ...], words: list[str]) -> bool:
    """Нечёткое совпадение строк проверяется по словам: каждое слово шаблона (кроме предлогов) и каждое
    значимое слово фразы должно найти пару — иначе «открой телефон» сошлось бы с «открой телеграм».
    Слово, разбитое распознаванием надвое («ю туб» ↔ «ютуб»), тоже считается парой."""
    joined = [a + b for a, b in pairwise(words)]
    for tw in template_words:
        if tw in SOFT_WORDS:
            continue
        need = word_threshold(tw)
        if not any(word_sim(tw, w) >= need for w in words) and not any(
            fuzz.ratio(tw, j) >= 88 for j in joined
        ):
            return False
    tjoined = [a + b for a, b in pairwise(template_words)]
    for i, w in enumerate(words):
        if len(w) <= 2 or w in SOFT_WORDS:
            continue
        if any(word_sim(tw, w) >= word_threshold(tw) for tw in template_words):
            continue
        if any(fuzz.ratio(w, j) >= 88 for j in tjoined):
            continue
        prev = words[i - 1] if i > 0 else ""
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if any(
            (nxt and fuzz.ratio(tw, w + nxt) >= 88) or (prev and fuzz.ratio(tw, prev + w) >= 88)
            for tw in template_words
        ):
            continue
        return False
    return True
