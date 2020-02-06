from types import MethodType
from typing import Optional, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from urllib.parse import urlparse

from django.http import QueryDict, HttpResponse, HttpRequest
from django.urls import Resolver404, resolve, ResolverMatch
from django.utils.functional import SimpleLazyObject


__all__ = ["IntercoolerData", "IntercoolerRedirector"]


# NOTE - HttpMethodOverride support for older browers removed
# can be replaced with https://pypi.python.org/pypi/django-method-override


@dataclass(frozen=True)
class NameId:
    name: Optional[str]
    id: Optional[str]


@dataclass(frozen=True)
class UrlMatch:
    url: Optional[str]
    match: Optional[ResolverMatch]


class IntercoolerQueryDict(QueryDict):
    @property
    def url(self) -> UrlMatch:
        url = self.get("ic-current-url", None)
        match = None
        if url is not None:
            url = url.strip()
            url = urlparse(url)
            if url.path:
                try:
                    match = resolve(url.path)
                except Resolver404:
                    pass
        return UrlMatch(url, match)

    current_url = url

    @property
    def element(self) -> NameId:
        return NameId(self.get("ic-element-name", None), self.get("ic-element-id", None))

    @property
    def id(self) -> int:
        # I know IC calls it a UUID internally, buts its just 1, incrementing.
        return int(self.get("ic-id", "0"))

    @property
    def request(self) -> bool:
        return bool(self.get("ic-request", None))

    @property
    def target_id(self) -> str:
        return self.get("ic-target-id", None)

    @property
    def trigger(self) -> NameId:
        return NameId(self.get("ic-trigger-name", None), self.get("ic-trigger-id", None))

    @property
    def prompt_value(self) -> str:
        return self.get("ic-prompt-value", None)

    def __repr__(self):
        props = ("id", "request", "target_id", "element", "trigger", "prompt_value", "url")
        attrs = ["{name!s}={val!r}".format(name=prop, val=getattr(self, prop)) for prop in props]
        return "<{cls!s}: {attrs!s}>".format(cls=self.__class__.__name__, attrs=", ".join(attrs))


@contextmanager
def _mutate_querydict(qd):
    qd._mutable = True
    yield qd
    qd._mutable = False


def intercooler_data(request: HttpRequest) -> IntercoolerQueryDict:
    if not hasattr(request, "_processed_intercooler_data"):
        IC_KEYS = [
            "ic-current-url",
            "ic-element-id",
            "ic-element-name",
            "ic-id",
            "ic-prompt-value",
            "ic-target-id",
            "ic-trigger-id",
            "ic-trigger-name",
            "ic-request",
        ]
        ic_qd = IntercoolerQueryDict("", encoding=request.encoding)
        if request.method in ("GET", "HEAD", "OPTIONS"):
            query_params = request.GET
        else:
            query_params = request.POST

        query_keys = tuple(query_params.keys())
        for ic_key in IC_KEYS:
            if ic_key in query_keys:
                # emulate how .get() behaves, because pop returns the
                # whole shebang.
                # For a little while, we need to pop data out of request.GET
                with _mutate_querydict(query_params) as REQUEST_DATA:
                    try:
                        removed = REQUEST_DATA.pop(ic_key)[-1]
                    except IndexError:
                        removed = []
                with _mutate_querydict(ic_qd) as IC_DATA:
                    IC_DATA.update({ic_key: removed})

        # Don't pop these ones off, so that decisions can be made for
        # handling _method
        ic_request = query_params.get("_method")
        with _mutate_querydict(ic_qd) as IC_DATA:
            IC_DATA.update({"_method": ic_request})

        # If HttpMethodOverride is in the middleware stack, this may
        # return True.
        IC_DATA.changed_method = getattr(request, "changed_method", False)
        request._processed_intercooler_data = ic_qd

    return request._processed_intercooler_data


def _maybe_intercooler(self: HttpRequest) -> bool:
    return self.META.get("HTTP_X_IC_REQUEST") == "true"


def _is_intercooler(self: HttpRequest) -> bool:
    return self.is_ajax() and self.maybe_intercooler()


class MiddlewareHelper:
    def __init__(self, get_response: Callable[..., HttpResponse]):
        self.get_response = get_response


class IntercoolerData(MiddlewareHelper):
    def __call__(self, request: HttpRequest) -> HttpResponse:
        # dynamically attach the methods/data to the request instance
        request.maybe_intercooler = MethodType(_maybe_intercooler, request)
        request.is_intercooler = MethodType(_is_intercooler, request)
        request.intercooler_data = SimpleLazyObject(lambda: intercooler_data(request))

        return self.get_response(request)


class IntercoolerRedirector(MiddlewareHelper):
    def __call__(self, request: HttpRequest) -> HttpResponse:
        response = self.get_response(request)
        if not request.is_intercooler():
            return response

        if 300 <= response.status_code < 400:
            if response.has_header("Location"):
                url = response["Location"]
                del response["Location"]
                new_resp = HttpResponse()
                for k, v in response.items():
                    new_resp[k] = v
                new_resp["X-IC-Redirect"] = url
                return new_resp

        return response
