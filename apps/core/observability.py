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
_request_method: ContextVar[str] = ContextVar("denstock_request_method", default="-")
_request_path: ContextVar[str] = ContextVar("denstock_request_path", default="-")
_request_user: ContextVar[str] = ContextVar("denstock_request_user", default="-")

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
        variables = (_request_id, _request_method, _request_path, _request_user)
        tokens = (
            _request_id.set(request_id),
            _request_method.set(request.method or "-"),
            _request_path.set(request.path or "-"),
            _request_user.set(_describe_user(request)),
        )
        try:
            response = self.get_response(request)
            response[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            for variable, token in zip(variables, tokens, strict=True):
                variable.reset(token)


class RequestContextFilter(logging.Filter):
    """Дополнить запись контекстом запроса; саму запись не меняет."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = getattr(record, "request_id", None) or _request_id.get()
        record.method = _request_method.get()
        record.path = _request_path.get()
        record.user = _request_user.get()
        request = getattr(record, "request", None)
        if request is not None:
            # django.request отдаёт запись вместе с самим запросом: для неё это
            # источник точнее контекста, который мог уже смениться.
            record.method = getattr(request, "method", None) or record.method
            record.path = getattr(request, "path", None) or record.path
            if record.user in ("-", "anonymous"):
                record.user = _describe_user(request)
        return True


class RedactingFormatter(logging.Formatter):
    """Собрать строку и вычистить из неё секреты вместе с traceback."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))
