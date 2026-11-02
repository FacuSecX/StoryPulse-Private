# ==========================================
# StoryPulse Private v3.0
# Created by FacuSecX
# https://github.com/FacuSecX/StoryPulse-Private
# ==========================================

"""Local, bounded navigation suggestions; this module never executes an action.

Compare a small action vocabulary with the current profile/group catalogue. The
caller must confirm suggestions and revalidate their destination before use.
Similarity is a ranking aid, not a probability or authorization decision.
"""
from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
import re
import unicodedata


@dataclass(frozen=True)
class Suggestion:
    action: str
    target_type: str | None
    target_id: str | None
    label: str


ACTIONS = frozenset({
    'stories', 'group_review', 'schedule', 'publications', 'highlights', 'add',
    'accounts', 'groups', 'schedules', 'backups', 'status', 'help', 'menu',
})
_WORDS = re.compile(r'[a-z0-9]+(?:[._][a-z0-9]+)*')
_USERNAME = re.compile(r'[A-Za-z0-9._]{1,30}\Z')
_VOCABULARY = {
    'review': ('revisar', 'revisa', 'revisame', 'rev', 'ver', 'buscar', 'busca',
               'consultar', 'mirar', 'chequear', 'comprobar'),
    'stories': ('historia', 'historias', 'hist', 'story', 'stories'),
    'groups': ('grupo', 'grupos', 'grup'),
    'schedule': ('programar', 'programa', 'agendar', 'agenda', 'prog'),
    'schedules': ('programacion', 'programaciones', 'programadas', 'programados'),
    'publications': ('publicacion', 'publicaciones', 'posts', 'post', 'pubs', 'publi'),
    'highlights': ('destacada', 'destacadas', 'destacados', 'highlights'),
    'add': ('agregar', 'agrega', 'anadir', 'anade', 'incorporar'),
    'accounts': ('cuenta', 'cuentas', 'perfiles', 'perfil', 'gestionar', 'gestion'),
    'backups': ('backup', 'backups', 'respaldo', 'respaldos'),
    'status': ('estado',),
    'help': ('ayuda', 'comandos'),
    'menu': ('menu', 'inicio', 'principal'),
}
_FILLER = frozenset({
    'a', 'al', 'de', 'del', 'el', 'la', 'las', 'los', 'un', 'una', 'en', 'para',
    'por', 'favor', 'porfavor', 'quiero', 'quisiera', 'necesito', 'me', 'mi',
    'mis', 'podes', 'podrias', 'puedes', 'puedo', 'que', 'y', 'descargar',
    'descarga', 'descargame', 'hacer', 'una', 'ultima', 'ultimas', 'ultimo',
    'ultimos', 'instagram', 'ig',
})
_NEGATIONS = frozenset({'no', 'nunca', 'jamas', 'tampoco'})
_DESTRUCTIVE = ('borrar', 'borra', 'eliminar', 'elimina', 'reiniciar', 'resetear',
                'reset', 'limpiar', 'limpia', 'quitar', 'remover', 'desactivar',
                'detener', 'parar', 'cancelar', 'cancela')


def _normalize(value: object) -> str:
    text = str(value).casefold()
    return ''.join(c for c in unicodedata.normalize('NFKD', text)
                   if not unicodedata.combining(c))


def _distance(a: str, b: str) -> int:
    """Levenshtein distance over short, bounded input words."""
    if len(a) > len(b):
        a, b = b, a
    previous = list(range(len(a) + 1))
    for row, right in enumerate(b, 1):
        current = [row]
        for column, left in enumerate(a, 1):
            current.append(min(current[-1] + 1, previous[column] + 1,
                               previous[column - 1] + (left != right)))
        previous = current
    return previous[-1]


def _similarity(left: str, right: str) -> float:
    if left == right:
        return 1.0
    if not left or not right:
        return 0.0
    return max(SequenceMatcher(None, left, right, autojunk=False).ratio(),
               1 - _distance(left, right) / max(len(left), len(right)))


def _word_kind(word: str) -> tuple[str, float] | None:
    exact = [(kind, 1.0) for kind, values in _VOCABULARY.items() if word in values]
    if exact:
        return exact[0]
    if len(word) < 4 or '.' in word or '_' in word:
        return None
    best = None
    for kind, values in _VOCABULARY.items():
        for value in values:
            if len(value) < 4 or abs(len(word) - len(value)) > 3:
                continue
            edits = _distance(word, value)
            budget = 1 if min(len(word), len(value)) <= 4 else 2
            if kind == 'review' and min(len(word), len(value)) >= 6:
                budget = 3
            score = _similarity(word, value)
            threshold = 0.71 if kind == 'review' else 0.76
            if edits <= budget and score >= threshold and (best is None or score > best[1]):
                best = (kind, score)
    return best


def _blocked(words: list[str]) -> bool:
    for word in words:
        if word in _NEGATIONS or word in _DESTRUCTIVE:
            return True
        if len(word) >= 5 and any(
            abs(len(word) - len(value)) <= 1 and _distance(word, value) <= 1
            for value in _DESTRUCTIVE if len(value) >= 5
        ):
            return True
    return False


def _forms(username: str, name: str) -> set[str]:
    values = set()
    for text in (username, name):
        words = re.findall(r'[a-z0-9]+', _normalize(text))
        if not words:
            continue
        values.update(word for word in words if len(word) >= 2)
        values.add(' '.join(words))
        values.add(''.join(words))
    return values


def _target_score(query: str, forms: set[str]) -> float:
    if not query:
        return 0.0
    query = ' '.join(re.findall(r'[a-z0-9]+', query))
    size = len(query.replace(' ', ''))
    best = 0.0
    for form in forms:
        if query == form:
            return 1.0
        if size < 3:
            continue
        if form.startswith(query) and size >= 3:
            score = 0.90 if size >= 4 else 0.84
        else:
            score = _similarity(query, form)
        best = max(best, score)
    threshold = 0.78 if size == 3 else 0.72
    return best if best >= threshold else 0.0


def _profiles(values) -> list[tuple[str, str, set[str]]]:
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict):
            continue
        username = str(item.get('username', '')).strip().lstrip('@')
        if not _USERNAME.fullmatch(username) or username.casefold() in seen:
            continue
        seen.add(username.casefold())
        name = str(item.get('nombre', '')).strip()[:100]
        result.append((username, username, _forms(username, name)))
    return result


def _groups(values) -> list[tuple[str, str, set[str]]]:
    result, seen = [], set()
    for item in values:
        if not isinstance(item, dict) or isinstance(item.get('id'), bool):
            continue
        raw_id = str(item.get('id', ''))
        if not raw_id.isascii() or not raw_id.isdigit() or len(raw_id) > 15:
            continue
        group_id = str(int(raw_id))
        if group_id == '0' or group_id in seen:
            continue
        name = ' '.join(str(item.get('nombre', '')).split())[:100]
        if not name:
            continue
        seen.add(group_id)
        result.append((group_id, name, _forms('', name)))
    return result


def propose_intents(text, profiles, groups, *, hint=None, limit=5) -> list[Suggestion]:
    """Return at most ``limit`` safe navigation proposals from current data.

    ``hint`` is a trusted action supplied by a registered command. Returned
    identifiers always come from the provided catalogues, never from free text.
    Empty results mean the caller should ask for clarification, not execute.
    """
    if not isinstance(text, str) or len(text) > 512 or not isinstance(limit, int):
        return []
    limit = max(0, min(limit, 10))
    if not limit or (hint is not None and hint not in ACTIONS):
        return []
    normalized = _normalize(text)
    tokens = list(_WORDS.finditer(normalized))
    words = [match.group() for match in tokens]
    profile_values, group_values = _profiles(profiles), _groups(groups)
    known_usernames = {row[0].casefold() for row in profile_values}
    explicit = {index for index, match in enumerate(tokens)
                if match.start() and normalized[match.start() - 1] == '@'
                and match.group() in known_usernames}
    if _blocked([word for index, word in enumerate(words) if index not in explicit]):
        return []
    kinds, remainder = set(), []
    all_forms = set().union(*(row[2] for row in profile_values + group_values))
    if hint is not None:
        remainder = words
    else:
        for index, word in enumerate(words):
            tail = ' '.join(words[index:])
            tail_forms = {' '.join(re.findall(r'[a-z0-9]+', tail)),
                          ''.join(re.findall(r'[a-z0-9]+', tail))}
            if kinds and (index in explicit or bool(tail_forms & all_forms)):
                remainder = words[index:]
                break
            if word in _FILLER:
                continue
            match = _word_kind(word)
            if match and not (match[0] == 'review' and 'review' in kinds):
                kinds.add(match[0])
            else:
                remainder = words[index:]
                break
    query = ' '.join(remainder)
    if hint is None and not kinds:
        return []

    if hint is not None:
        action = hint
    elif 'add' in kinds:
        action = 'add'
    elif 'schedule' in kinds:
        action = 'schedule'
    elif 'highlights' in kinds:
        action = 'highlights'
    elif 'publications' in kinds:
        action = 'publications'
    elif any(kind in kinds for kind in ('schedules', 'backups', 'status', 'help', 'menu')):
        action = next(kind for kind in ('schedules', 'backups', 'status', 'help', 'menu')
                      if kind in kinds)
    elif 'accounts' in kinds and not query and 'stories' not in kinds:
        action = 'accounts'
    elif 'stories' in kinds or 'review' in kinds:
        action = 'stories'
    else:
        action = next((kind for kind in ('schedules', 'backups', 'status', 'help',
                                        'menu', 'groups', 'accounts') if kind in kinds), None)
    if action is None:
        return []

    labels = {
        'stories': 'Revisar historias', 'groups': 'Revisar grupos',
        'group_review': 'Revisar grupos', 'schedule': 'Programar revisión',
        'publications': 'Descargar publicaciones', 'highlights': 'Historias destacadas',
        'add': 'Agregar un perfil', 'accounts': 'Gestionar cuentas',
        'schedules': 'Ver programaciones', 'backups': 'Abrir backups',
        'status': 'Consultar estado', 'help': 'Ver ayuda', 'menu': 'Menú principal',
    }
    if action in {'add', 'accounts', 'schedules', 'backups', 'status', 'help', 'menu'}:
        if query and action != 'add':
            return []
        return [Suggestion(action, None, None, labels[action])]

    group_only = 'groups' in kinds or action in {'groups', 'group_review'}
    if group_only and action in {'publications', 'highlights'}:
        return []
    if not query:
        if group_only and action == 'stories':
            action = 'groups'
        return [Suggestion(action, None, None, labels[action])]

    candidates = []
    if not group_only:
        for target_id, name, forms in profile_values:
            score = _target_score(query, forms)
            if score:
                candidates.append((score, 'profile', target_id, name))
    if group_only or action in {'stories', 'schedule'}:
        for target_id, name, forms in group_values:
            score = _target_score(query, forms)
            if score:
                candidates.append((score, 'group', target_id, name))
    candidates.sort(key=lambda row: (-row[0], row[1], row[3].casefold(), row[2]))
    if candidates:
        candidates = [row for row in candidates if row[0] >= candidates[0][0] - 0.18]
    suggestions = []
    for _, kind, target_id, name in candidates[:limit]:
        target_action = 'group_review' if kind == 'group' and action != 'schedule' else action
        if target_action == 'groups':
            target_action = 'group_review'
        prefix = labels.get(target_action, 'Revisar grupo')
        if target_action == 'group_review':
            prefix = 'Revisar grupo'
        target_label = '@' + name if kind == 'profile' else name
        suggestions.append(Suggestion(target_action, kind, target_id,
                                      f'{prefix}: {target_label}'))
    if suggestions and action == 'stories' and len(suggestions) < limit:
        first = suggestions[0]
        display = ('@' + first.target_id if first.target_type == 'profile'
                   else next(row[3] for row in candidates if row[1] == 'group' and row[2] == first.target_id))
        suggestions.append(Suggestion('schedule', first.target_type, first.target_id,
                                      f'Programar revisión: {display}'))
    return suggestions[:limit]
