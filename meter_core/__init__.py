from .lcr2 import LCR2Register
from .lcr600 import LCR600Register
from .lcriq import LCRiQRegister
from .register_base import RegisterError, PRODUCT_SPECS

REGISTER_CLASSES = {
    "lcr2": LCR2Register,
    "lcr600": LCR600Register,
    "lcriq": LCRiQRegister,
}

__all__ = ["LCR2Register", "LCR600Register", "LCRiQRegister",
           "RegisterError", "PRODUCT_SPECS", "REGISTER_CLASSES"]
