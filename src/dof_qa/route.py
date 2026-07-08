"""Question routing: which questions are SQL, which are retrieval, which are both.

This is the part that is actually hard, and the reason this is not a vector
store with a prompt on top. The DOF corpus is hybrid — every notice has strict
structured metadata (date, edition, section, branch, issuing agency) *and*
unstructured legal text — so questions fall into three shapes that need
different machinery:

    "¿Cuántas notas publicó la SHCP en julio?"
        -> pure SQL. Retrieval would return 10 passages and no count. A RAG
           system that answers this by summarising retrieved chunks is
           guessing at arithmetic it could have computed exactly.

    "¿Qué dice el DOF sobre dispositivos médicos?"
        -> pure retrieval. No structured predicate to apply.

    "¿Qué publicó COFEPRIS sobre registros sanitarios este trimestre?"
        -> both, and the order matters. The agency and the date range must
           become a *pre-filter* on the candidate set, not a post-filter on
           BM25 results. Post-filtering means the top-k is chosen globally and
           then decimated: ask for k=10 and you may keep 1. Pre-filtering means
           k=10 within the eligible set. On a corpus where one agency is 2% of
           the rows, that difference is most of the recall.

Everything here is deterministic and dependency-free. No LLM decides the route,
which means routing is unit-testable, reproducible in CI, and cannot fail in a
new way between runs.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date

from .index import fold

# --------------------------------------------------------------------------
# domain vocabulary
# --------------------------------------------------------------------------

# Users type acronyms; the DOF prints full legal names. "COFEPRIS" appears in
# the *body* of notices but the `organismo` column says "COMISION FEDERAL PARA
# LA PROTECCION CONTRA RIESGOS SANITARIOS". Without this mapping, the single
# most natural way to ask a question about an agency matches nothing.
#
# Health-regulatory entries first, since that is the domain this system is
# aimed at; the rest are the agencies that dominate DOF volume.
ACRONYMS: dict[str, str] = {
    "cofepris": "comision federal para la proteccion contra riesgos sanitarios",
    "conamed": "comision nacional de arbitraje medico",
    "cenetec": "centro nacional de excelencia tecnologica en salud",
    "conbioetica": "comision nacional de bioetica",
    "censida": "centro nacional para la prevencion y el control del vih",
    "cofece": "comision federal de competencia economica",
    "imss": "instituto mexicano del seguro social",
    "issste": "instituto de seguridad y servicios sociales de los trabajadores del estado",
    "ssa": "secretaria de salud",
    "senasica": "servicio nacional de sanidad inocuidad y calidad agroalimentaria",
    "cnpss": "comision nacional de proteccion social en salud",
    "shcp": "secretaria de hacienda y credito publico",
    "segob": "secretaria de gobernacion",
    "sre": "secretaria de relaciones exteriores",
    "sep": "secretaria de educacion publica",
    "semarnat": "secretaria de medio ambiente y recursos naturales",
    "sener": "secretaria de energia",
    "banxico": "banco de mexico",
    "cfe": "comision federal de electricidad",
    "fgr": "fiscalia general de la republica",
    "sat": "servicio de administracion tributaria",
    "profeco": "procuraduria federal del consumidor",
    "dif": "sistema nacional para el desarrollo integral de la familia",
    "inai": "instituto nacional de transparencia",
    "cndh": "comision nacional de los derechos humanos",
}

# Domain synonyms. Legal Spanish is formulaic: a "registro sanitario" is never
# called a "permiso de salud" in the DOF, but a user might ask that way.
SYNONYMS: dict[str, tuple[str, ...]] = {
    "dispositivo medico": ("dispositivos medicos", "insumos para la salud"),
    "registro sanitario": ("registros sanitarios",),
    "medicamento": ("medicamentos", "farmacos", "insumos para la salud"),
    "farmacovigilancia": ("farmacovigilancia",),
    "norma oficial": ("norma oficial mexicana", "nom"),
    "licitacion": ("licitacion publica", "convocatoria"),
    "tarifa": ("tarifas",),
}

MONTHS = {
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4, "mayo": 5, "junio": 6,
    "julio": 7, "agosto": 8, "septiembre": 9, "setiembre": 9, "octubre": 10,
    "noviembre": 11, "diciembre": 12,
}

# Words that carry no retrieval signal in Spanish question form. Kept small on
# purpose: aggressive stopword lists remove legal terms of art ("de" matters in
# "Secretaría de Salud"). See the ablation — a bigger list measurably hurt.
STOPWORDS = frozenset(
    """
    a abril acerca agosto al algun alguna alguno ano anos aquel bimestre busca
    buscar como con conteo cual cuales cualquier cuando cuanta cuantas cuanto
    cuantos cuarto cuenta dame de del desglosa desglose dia diario dias dice
    dicen diciembre dime dof donde edicion el emitieron emitio en encuentra
    enero enumera es esa ese eso esta estas este estos existe existen febrero
    fecha federacion fue ha han hay haya julio junio la las le lista listado
    listar lo los marzo mayo me mes meses muestrame nos nota notas noviembre
    numero o octubre oficial para pasado periodo por porque primer primero
    promedio publicacion publicaciones publicado publicaron publico que quien
    quienes reciente recientes relacionado relativa relativo respecto se segundo
    semana semanas semestre septiembre setiembre sin sobre son su sus te tercer
    tercero total totales trimestre u ultimo ultimos un una unas unos y
    """.split()
)
# Block 2 is corpus vocabulary, block 3 is routing vocabulary. Every document here is a
# "nota" in the "Diario Oficial de la Federación", so those words carry zero
# discriminative signal -- they match everything. Leaving them in was what made
# "¿cuántas notas publicó la SHCP?" route to retrieval instead of SQL: one
# meaningless term was enough to look like a topical question.
#
# Block 3 is temporal and aggregate vocabulary. Those words are *already
# consumed* by `extract_dates` and the aggregate-cue regex; leaving them in
# `terms` as well made every counting question look topical and routed
# "¿cuántas notas publicó la CFE en julio de 2026?" to hybrid instead of SQL.
# A signal must be read by exactly one extractor.

AGGREGATE_CUES = re.compile(
    r"\b(cuant[oa]s?|numero de|total de|conteo|cuenta|promedio|"
    r"lista completa|listado|enumera|desglos)", re.I
)
EXISTENCE_CUES = re.compile(r"\b(hay|existe[n]?|public[oó]|publicaron|se public)", re.I)


# --------------------------------------------------------------------------
# the plan
# --------------------------------------------------------------------------

Strategy = str  # "sql" | "retrieval" | "hybrid"


@dataclass(slots=True)
class Filters:
    organismos: list[str] = field(default_factory=list)  # folded substrings
    fecha_desde: str | None = None
    fecha_hasta: str | None = None

    def is_empty(self) -> bool:
        return not self.organismos and not self.fecha_desde and not self.fecha_hasta

    def describe(self) -> str:
        bits = []
        if self.organismos:
            bits.append("organismo~" + "|".join(self.organismos))
        if self.fecha_desde or self.fecha_hasta:
            bits.append(f"fecha {self.fecha_desde or '*'}..{self.fecha_hasta or '*'}")
        return ", ".join(bits) or "sin filtros"


@dataclass(slots=True)
class Plan:
    question: str
    strategy: Strategy
    filters: Filters
    terms: list[str]                       # folded content terms
    expansions: dict[str, list[str]] = field(default_factory=dict)
    wants_count: bool = False
    reason: str = ""

    def describe(self) -> str:
        return (
            f"strategy={self.strategy} | {self.filters.describe()} | "
            f"terms={self.terms or '-'} | {self.reason}"
        )


# --------------------------------------------------------------------------
# extraction
# --------------------------------------------------------------------------


def _strip_accents(s: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    )


def extract_dates(question: str, today: date | None = None) -> tuple[str | None, str | None]:
    """Resolve Spanish temporal expressions to an ISO range.

    `today` is injectable because a date-dependent function that reads the
    clock is a function you cannot test. Every golden question with a relative
    date is evaluated against a pinned date.
    """
    today = today or date.today()
    q = fold(question)

    m = re.search(r"\b(entre|del?)\s+(\d{4})\s*(?:y|a|al|hasta)\s*(\d{4})", q)
    if m:
        return f"{m.group(2)}-01-01", f"{m.group(3)}-12-31"

    m = re.search(r"\b(\d{1,2})\s*de\s*([a-z]+)\s*(?:de\s*)?(\d{4})", q)
    if m and m.group(2) in MONTHS:
        d = f"{int(m.group(3)):04d}-{MONTHS[m.group(2)]:02d}-{int(m.group(1)):02d}"
        return d, d

    m = re.search(r"\b([a-z]+)\s+(?:de\s+)?(\d{4})\b", q)
    if m and m.group(1) in MONTHS:
        y, mo = int(m.group(2)), MONTHS[m.group(1)]
        return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{_month_end(y, mo):02d}"

    for name, mo in MONTHS.items():
        if re.search(rf"\b(?:en|durante|de)\s+{name}\b", q):
            y = today.year
            return f"{y}-{mo:02d}-01", f"{y}-{mo:02d}-{_month_end(y, mo):02d}"

    m = re.search(r"\bultim[oa]s?\s+(\d{1,4})\s*(dias?|semanas?|mes(?:es)?|anos?)", q)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        days = n * {"d": 1, "s": 7, "m": 30, "a": 365}[unit[0]]
        return (today.fromordinal(today.toordinal() - days)).isoformat(), today.isoformat()

    if re.search(r"\beste\s+trimestre\b", q):
        q_start_month = 3 * ((today.month - 1) // 3) + 1
        return (
            f"{today.year}-{q_start_month:02d}-01",
            f"{today.year}-{q_start_month + 2:02d}-{_month_end(today.year, q_start_month + 2):02d}",
        )
    m = re.search(r"\b(primer|segundo|tercer|cuarto)\s+trimestre(?:\s+de\s+(\d{4}))?", q)
    if m:
        ix = ["primer", "segundo", "tercer", "cuarto"].index(m.group(1))
        y = int(m.group(2)) if m.group(2) else today.year
        start, end = 3 * ix + 1, 3 * ix + 3
        return f"{y}-{start:02d}-01", f"{y}-{end:02d}-{_month_end(y, end):02d}"

    if re.search(r"\beste\s+(ano|year)\b", q):
        return f"{today.year}-01-01", f"{today.year}-12-31"
    if re.search(r"\bano\s+pasado\b", q):
        return f"{today.year - 1}-01-01", f"{today.year - 1}-12-31"

    m = re.search(r"\b(?:en|de|durante)\s+(20\d{2})\b", q)
    if m:
        return f"{m.group(1)}-01-01", f"{m.group(1)}-12-31"

    return None, None


def _month_end(year: int, month: int) -> int:
    if month == 2:
        leap = year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
        return 29 if leap else 28
    return 30 if month in (4, 6, 9, 11) else 31


def extract_agencies(question: str, known: list[str]) -> list[str]:
    """Match agencies by acronym or by name overlap against the real corpus.

    `known` is `SELECT DISTINCT organismo FROM nota`, so we only ever produce
    filters that can match something. Inventing a filter that matches nothing
    turns a retrievable question into a refusal.
    """
    q = fold(question)
    hits: list[str] = []

    for acro, full in ACRONYMS.items():
        if re.search(rf"\b{re.escape(acro)}\b", q):
            for org in known:
                folded = fold(org)
                # Either the corpus agency matches the expansion, or (for
                # agencies not in this corpus) keep the expansion itself so the
                # filter is still meaningful once the corpus grows.
                if _overlap(folded, full) >= 0.6:
                    hits.append(folded)
            if not hits:
                hits.append(full)

    for org in known:
        folded = fold(org)
        # A full agency name quoted in the question.
        if len(folded) > 12 and folded in q:
            hits.append(folded)

    # Agencies we know exist in Mexico but that this corpus does not contain.
    # Matching them anyway is what turns "¿qué publicó la Secretaría de Salud?"
    # into an honest "that agency is not in the corpus" instead of an unfiltered
    # lexical search that returns whatever happens to share vocabulary. A filter
    # that matches nothing is more useful than no filter at all -- it is what
    # the coverage gate needs in order to refuse for the right reason.
    if not hits:
        for full in ACRONYMS.values():
            if len(full) > 12 and full in q:
                hits.append(full)
                break

    seen, out = set(), []
    for h in hits:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _overlap(a: str, b: str) -> float:
    """Jaccard over content words. Cheap, and enough to align legal names."""
    wa = {w for w in a.split() if w not in STOPWORDS and len(w) > 2}
    wb = {w for w in b.split() if w not in STOPWORDS and len(w) > 2}
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def extract_terms(question: str, agencies: list[str]) -> list[str]:
    """Content words left after removing stopwords and agency mentions."""
    q = fold(question)
    for acro in ACRONYMS:
        q = re.sub(rf"\b{re.escape(acro)}\b", " ", q)
    for agency in agencies:
        q = q.replace(agency, " ")
    q = re.sub(r"[^\w\s]", " ", q)
    terms = [w for w in q.split() if w not in STOPWORDS and len(w) > 2 and not w.isdigit()]
    seen, out = set(), []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def expand_terms(terms: list[str], question: str) -> dict[str, list[str]]:
    """Domain synonym expansion, recorded so the ablation can turn it off."""
    q = fold(question)
    out: dict[str, list[str]] = {}
    for phrase, alts in SYNONYMS.items():
        if phrase in q or all(w in terms for w in phrase.split()):
            out[phrase] = list(alts)
    for acro, full in ACRONYMS.items():
        if re.search(rf"\b{re.escape(acro)}\b", q):
            out[acro] = [full]
    return out


# --------------------------------------------------------------------------
# routing
# --------------------------------------------------------------------------


def plan(question: str, known_agencies: list[str], today: date | None = None) -> Plan:
    """Turn a natural-language question into an execution plan."""
    agencies = extract_agencies(question, known_agencies)
    desde, hasta = extract_dates(question, today)
    filters = Filters(organismos=agencies, fecha_desde=desde, fecha_hasta=hasta)
    terms = extract_terms(question, agencies)
    expansions = expand_terms(terms, question)

    wants_count = bool(AGGREGATE_CUES.search(_strip_accents(question)))

    # The routing decision itself. Three rules, in priority order.
    if wants_count and not terms:
        strategy, reason = "sql", "aggregate cue, no topical terms -> exact count beats retrieval"
    elif not terms and not filters.is_empty():
        strategy, reason = "sql", "structured predicate only -> enumerate, do not rank"
    elif terms and filters.is_empty():
        strategy, reason = "retrieval", "topical terms, no structured predicate"
    elif terms:
        strategy, reason = "hybrid", "structured predicate pre-filters the retrieval candidates"
    else:
        strategy, reason = "retrieval", "no signal extracted; fall back to lexical search"

    return Plan(
        question=question,
        strategy=strategy,
        filters=filters,
        terms=terms,
        expansions=expansions,
        wants_count=wants_count,
        reason=reason,
    )
