"""业务错误：被各后端层（规则、复核服务、HTTP 层）共用。"""
from __future__ import annotations


class BusinessError(Exception):
    def __init__(self, message, status=400, code="bad_request"):
        super().__init__(message)
        self.message, self.status, self.code = message, status, code
