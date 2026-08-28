"""Диагностический след для ошибок 500.

DEBUG в бою выключен, и до этого модуля падение отдавало оператору страницу
ошибки, не оставляя в журналах ничего: обработчик консоли у Django отфильтрован
по DEBUG, а письма администраторам никуда не идут. Разобрать жалобу «нажал и
всё сломалось» было не по чему.

Здесь ровно то, что нужно для разбора, и ничего сверх: время, уровень, имя
логгера, метод и путь запроса, кто его сделал, сквозной номер запроса, процесс
и поток, и сам traceback. Тело запроса, cookies, заголовки авторизации, пароли,
токены и идентификатор сессии в запись не попадают: в строку идут только
перечисленные поля, а готовый текст, вместе с traceback, проходит через замену
по ключевым словам - на случай, если секрет придёт внутри самого сообщения.
"""
import logging
import re
import uuid
from contextvars import ContextVar

_request_id: ContextVar[str] = ContextVar("denstock_request_id", default="-")
_current_request: ContextVar[object | None] = ContextVar(
    "denstock_current_request", default=None
)

REQUEST_ID_HEADER = "X-Request-ID"
REDACTED = "[скрыто]"

# Слова, после которых в тексте может оказаться секрет. Вырезается значение,
# сам ключ остаётся: по нему видно, что здесь было, и что оно убрано.
_SECRET_KEYS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "api[-_]?key",
    "csrfmiddlewaretoken",
    "csrftoken",
    "sessionid",
    "cookie",
    "signature",
)
# Значение бывает и в кавычках с пробелами внутри («Bearer xyz»), поэтому
# кавычки съедаются целиком, а не обрываются на первом пробеле.
_SECRET_PATTERN = re.compile(
    r"(?i)(\"?\b(?:" + "|".join(_SECRET_KEYS) + r")\b\"?\s*[=:]\s*)"
    r"(\"[^\"]*\"|'[^']*'|[^\s,;&)}\]]+)"
)


def redact(text: str) -> str:
    """Убрать значения секретов, оставив имена полей."""
    return _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{REDACTED}", text)


def current_request_id() -> str:
    return _request_id.get()


def _describe_user(request) -> str:
    """Кто сделал запрос: id и логин. Ни почты, ни телефона, ни прав."""
    user = getattr(request, "user", None)
    if user is None or not getattr(user, "is_authenticated", False):
        return "anonymous"
    return f"{user.pk}:{user.get_username()}"


class RequestIdMiddleware:
    """Сквозной номер запроса: по нему собирается вся картина одного падения."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        request_id = str(uuid.uuid4())
        request.request_id = request_id
        # В переменную кладётся сам запрос, а не готовая строка: этот слой стоит
        # первым, до аутентификации, и снятый здесь пользователь всегда был бы
        # анонимным. Кто это, спрашиваем в момент записи в журнал.
        id_token = _request_id.set(request_id)
        request_token = _current_request.set(request)
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            _current_request.reset(request_token)
            _request_id.reset(id_token)


class RequestContextFilter(logging.Filter):
    """Дополнить запись контекстом запроса; саму запись не меняет."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", None) or _request_id.get()
        # django.request отдаёт запись вместе с самим запросом; для остальных
        # логгеров берём тот, что обрабатывается сейчас.
        request = getattr(record, "request", None) or _current_request.get()
        record.method = getattr(request, "method", None) or "-"
        record.path = getattr(request, "path", None) or "-"
        record.user = _describe_user(request) if request is not None else "-"
        return True


class RedactingFormatter(logging.Formatter):
    """Собрать строку и вычистить из неё секреты вместе с traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
