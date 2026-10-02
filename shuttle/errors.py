class ShuttleError(Exception):
    """业务错误基类，消息可直接展示给用户。"""


class AuthzError(ShuttleError):
    """越权：运营员操作未授权线路 / 司机改动核销等。"""


class CapacityError(ShuttleError):
    """区间容量不足。"""


class ConflictError(ShuttleError):
    """状态冲突：重复预约、已核销、已发车不可改等。"""


class ValidationError(ShuttleError):
    """入参不合法：站点顺序错、站点不属本班次等。"""
